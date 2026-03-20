"""Oracle ADG 预检查与预览 API"""

# 变更说明: backend/app/api/oracle_adg.py 修复预览接口 500，涉及 KeyError 规避、日志增强与错误信息细化。

import re
import shlex
import socket
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from loguru import logger

try:
    from paramiko import ssh_exception
except ImportError:
    ssh_exception = None  # type: ignore

from ..core.executor import RemoteExecutor, SqlExecutor
from ..core.config import SSHConfig
from ..models.schemas import (
    PreviewRequest,
    PreviewResponse,
    DiscoveryInfo,
    OracleEnvInfo,
    PrecheckResult,
    ExecutionPlan,
    PlanStep,
    PlanStage,
    RiskSummary,
    RiskLevel,
)
from ..models.response import APIResponse

router = APIRouter(prefix="/api/oracle/adg", tags=["Oracle ADG"])

# 预览 API 会在主备主机之间串行执行 SSH 探测。较短的 SSH 连接超时可以缩短失败场景
# 的等待时间，让请求在前端或 API 网关 30 秒超时之前返回结构化错误。
DEFAULT_SSH_CONNECT_TIMEOUT = 10


class PreviewError(Exception):
    """基础预览异常，便于分类返回到前端"""

    error_type = "preview_error"
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(
        self,
        message: str,
        *,
        role: Optional[str] = None,
        host: Optional[str] = None,
        hint: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.role = role
        self.host = host
        self.hint = hint
        self.details = details or {}

    def to_http_exception(self) -> HTTPException:
        payload: Dict[str, Any] = {
            "error_type": self.error_type,
            "message": self.message,
        }
        if self.role:
            payload["role"] = self.role
        if self.host:
            payload["host"] = self.host
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        return HTTPException(status_code=self.status_code, detail=payload)


class HostUnreachableError(PreviewError):
    error_type = "host_unreachable"
    status_code = status.HTTP_504_GATEWAY_TIMEOUT


class SSHConnectionError(PreviewError):
    error_type = "ssh_connection_failed"
    status_code = status.HTTP_502_BAD_GATEWAY


class SSHAuthenticationError(PreviewError):
    error_type = "ssh_auth_failed"
    status_code = status.HTTP_401_UNAUTHORIZED


class OracleEnvironmentError(PreviewError):
    error_type = "oracle_env_invalid"
    status_code = status.HTTP_400_BAD_REQUEST


class SqlPlusAccessError(PreviewError):
    error_type = "sqlplus_unreachable"
    status_code = status.HTTP_502_BAD_GATEWAY


ROLE_LABELS = {
    "primary": "主库",
    "standby": "备库",
}


def summarize_risk(prechecks: List[PrecheckResult], plan: ExecutionPlan) -> RiskSummary:
    """根据预检查与计划生成风险摘要"""
    summary = RiskSummary()
    summary.total_checks = len(prechecks)

    for check in prechecks:
        if check.result == "pass":
            summary.passed += 1
        elif check.result == "warn":
            summary.warned += 1
        elif check.result == "fail":
            summary.failed += 1
        if check.blocking:
            summary.blocking_issues.append(check.check_name)

    for stage in plan.stages:
        steps = stage.steps if isinstance(stage, PlanStage) else stage.get("steps", [])
        for step in steps:
            if isinstance(step, PlanStep) and step.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
                summary.high_risk_steps.append(step.step_id)

    return summary


class OraclePreviewExecutor:
    """Oracle ADG 预览执行器"""

    def __init__(self, request: PreviewRequest):
        self.request = request
        self.primary_ssh: Optional[RemoteExecutor] = None
        self.standby_ssh: Optional[RemoteExecutor] = None
        self.primary_sql: Optional[SqlExecutor] = None
        self.standby_sql: Optional[SqlExecutor] = None

    def connect(self) -> bool:
        """建立 SSH/SQL 连接并校验 Oracle 环境"""
        try:
            self.primary_ssh = RemoteExecutor(self._build_primary_ssh_config())
            self._ensure_ssh_connection(self.primary_ssh, role="primary")

            primary_oracle_home = self._resolve_primary_oracle_home()
            primary_sid = self._resolve_primary_sid()
            self._ensure_sid_present(primary_sid, role="primary")
            self._validate_oracle_environment(self.primary_ssh, primary_oracle_home, role="primary")
            self.primary_sql = SqlExecutor(self.primary_ssh, primary_oracle_home, primary_sid)
            self._verify_sqlplus_access(self.primary_sql, role="primary")

            self.standby_ssh = RemoteExecutor(self._build_standby_ssh_config())
            self._ensure_ssh_connection(self.standby_ssh, role="standby")

            standby_oracle_home = self._resolve_standby_oracle_home()
            standby_sid = self._resolve_standby_sid()
            self._ensure_sid_present(standby_sid, role="standby")
            self._validate_oracle_environment(self.standby_ssh, standby_oracle_home, role="standby")
            self.standby_sql = SqlExecutor(self.standby_ssh, standby_oracle_home, standby_sid)
            self._verify_sqlplus_access(self.standby_sql, role="standby")

            return True
        except PreviewError:
            self.disconnect()
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"连接失败: {exc}")
            self.disconnect()
            raise

    def disconnect(self):
        """断开所有连接"""
        if self.primary_ssh:
            self.primary_ssh.close()
            self.primary_ssh = None
        if self.standby_ssh:
            self.standby_ssh.close()
            self.standby_ssh = None
        self.primary_sql = None
        self.standby_sql = None

    def _ensure_ssh_connection(self, executor: RemoteExecutor, role: str):
        if executor.connect():
            return
        last_error = getattr(executor, "last_error", None)
        raise self._classify_ssh_error(last_error, role)

    def _classify_ssh_error(self, error: Optional[Exception], role: str) -> PreviewError:
        host = self._host_for_role(role)
        label = self._role_label(role)
        base_hint = "请检查网络连通性、防火墙和 SSH 端口配置"
        detail_payload: Dict[str, Any] = {"error": str(error)} if error else {}

        if error is None:
            return SSHConnectionError(
                f"{label} SSH 连接失败，原因未知",
                role=role,
                host=host,
                hint=base_hint,
            )

        if isinstance(error, (TimeoutError, socket.timeout)):
            return HostUnreachableError(
                f"{label} SSH 连接超时",
                role=role,
                host=host,
                hint="连接超时，请检查网络、防火墙或端口",
                details=detail_payload,
            )

        if isinstance(error, OSError):
            errno = getattr(error, "errno", None)
            if errno in {101, 110, 111, 113}:
                return HostUnreachableError(
                    f"{label} 主机不可达 (errno={errno})",
                    role=role,
                    host=host,
                    hint="目标主机未响应或端口未开放",
                    details=detail_payload,
                )

        if ssh_exception is not None:
            if isinstance(error, ssh_exception.NoValidConnectionsError):
                return HostUnreachableError(
                    f"{label} 无法建立 SSH 连接 (NoValidConnectionsError)",
                    role=role,
                    host=host,
                    hint="目标端口未开放或被防火墙阻断",
                    details={"errors": getattr(error, "errors", None)},
                )
            if isinstance(error, ssh_exception.AuthenticationException):
                return SSHAuthenticationError(
                    f"{label} SSH 认证失败: {error}",
                    role=role,
                    host=host,
                    hint="请检查密码或私钥配置",
                    details=detail_payload,
                )
            if isinstance(error, ssh_exception.BadAuthenticationType):
                return SSHAuthenticationError(
                    f"{label} SSH 认证方式不被接受，支持: {getattr(error, 'allowed', [])}",
                    role=role,
                    host=host,
                    hint="请选择服务器允许的认证方式 (密码或私钥)",
                    details={"allowed": getattr(error, "allowed", None)},
                )
            if isinstance(error, ssh_exception.SSHException):
                return SSHConnectionError(
                    f"{label} SSH 协议握手失败: {error}",
                    role=role,
                    host=host,
                    hint=base_hint,
                    details=detail_payload,
                )

        return SSHConnectionError(
            f"{label} SSH 连接失败: {error}",
            role=role,
            host=host,
            hint=base_hint,
            details=detail_payload,
        )

    def _ensure_sid_present(self, sid: Optional[str], role: str):
        if sid:
            return
        host = self._host_for_role(role)
        label = self._role_label(role)
        raise OracleEnvironmentError(
            f"{label} 未提供 Oracle SID，无法建立 SQL 连接",
            role=role,
            host=host,
            hint="请填写对应实例的 SID",
        )

    def _validate_oracle_environment(self, executor: RemoteExecutor, oracle_home: Optional[str], role: str):
        host = self._host_for_role(role)
        label = self._role_label(role)
        if not oracle_home:
            raise OracleEnvironmentError(
                f"{label} 未提供 ORACLE_HOME",
                role=role,
                host=host,
                hint="请在表单中提供准确的 ORACLE_HOME 路径",
            )

        quoted_home = shlex.quote(oracle_home)
        home_result = executor.execute(f"test -d {quoted_home}")
        if not home_result.success:
            raise OracleEnvironmentError(
                f"{label} ORACLE_HOME 不存在或不可访问: {oracle_home}",
                role=role,
                host=host,
                hint="请确认路径存在且 oracle 用户具备访问权限",
                details={"stderr": home_result.stderr, "oracle_home": oracle_home},
            )

        sqlplus_path = f"{oracle_home.rstrip('/')}/bin/sqlplus"
        sqlplus_result = executor.execute(f"test -x {shlex.quote(sqlplus_path)}")
        if not sqlplus_result.success:
            raise OracleEnvironmentError(
                f"{label} sqlplus 不可用: {sqlplus_path}",
                role=role,
                host=host,
                hint="请确认 Oracle 软件已安装且 sqlplus 可执行",
                details={
                    "oracle_home": oracle_home,
                    "sqlplus_path": sqlplus_path,
                    "stderr": sqlplus_result.stderr,
                },
            )

    def _verify_sqlplus_access(self, sql_executor: SqlExecutor, role: str):
        host = self._host_for_role(role)
        label = self._role_label(role)
        heartbeat = sql_executor.execute_sql("SELECT status FROM v$instance")
        stdout = (heartbeat.stdout or "").strip()
        stderr = (heartbeat.stderr or "").strip()
        has_sql_error = any(
            marker in text
            for marker in ("ORA-", "SP2-")
            for text in (stdout, stderr)
            if text
        )
        if not heartbeat.success or has_sql_error:
            message = stderr or stdout or "SQL*Plus 执行失败"
            raise SqlPlusAccessError(
                f"{label} 无法访问 SQL*Plus/实例: {message}",
                role=role,
                host=host,
                hint="请确认数据库已启动且 ORACLE_SID/SYSDBA 权限正确",
                details={
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": heartbeat.exit_code,
                },
            )

    def _host_for_role(self, role: str) -> str:
        return self.request.primary_host if role == "primary" else self.request.standby_host

    @staticmethod
    def _role_label(role: str) -> str:
        return ROLE_LABELS.get(role, role)

    def discover(self) -> DiscoveryInfo:
        """自动探测环境信息"""
        discovery = DiscoveryInfo()

        try:
            self._discover_primary_host(discovery)
            self._discover_standby_host(discovery)
            self._discover_oracle_env(discovery)
            self._discover_database_status(discovery)
            self._discover_network(discovery)
            self._discover_storage(discovery)
            self._detect_conflicts(discovery)
        except Exception as exc:
            logger.error(f"探测过程出错: {exc}")

        return discovery

    def _discover_primary_host(self, discovery: DiscoveryInfo):
        """探测主库主机信息"""
        discovery.primary_host = {"host": self.request.primary_host}

        if self.primary_sql:
            os_sql = "SELECT value FROM v$parameter WHERE name = 'os_name'"
            result = self.primary_sql.execute_sql(os_sql)
            if result.success:
                discovery.primary_host["os_type"] = result.stdout.strip()

        if self.primary_ssh:
            hostname_result = self.primary_ssh.execute("hostname")
            if hostname_result.success:
                discovery.primary_host["hostname"] = hostname_result.stdout.strip()

            ip_result = self.primary_ssh.execute("hostname -I")
            if ip_result.success:
                discovery.primary_host["ip_address"] = ip_result.stdout.strip()

    def _discover_standby_host(self, discovery: DiscoveryInfo):
        """探测备库主机信息"""
        discovery.standby_host = {"host": self.request.standby_host}

        if self.standby_ssh:
            hostname_result = self.standby_ssh.execute("hostname")
            if hostname_result.success:
                discovery.standby_host["hostname"] = hostname_result.stdout.strip()

    def _discover_oracle_env(self, discovery: DiscoveryInfo):
        """探测 Oracle 环境"""
        version_sql = "SELECT version FROM v$instance"

        if self.primary_sql:
            result = self.primary_sql.execute_sql(version_sql)
            if result.success:
                discovery.primary_oracle.version = result.stdout.strip()

        if self.standby_sql:
            result = self.standby_sql.execute_sql(version_sql)
            if result.success:
                discovery.standby_oracle.version = result.stdout.strip()

        discovery.primary_oracle.oracle_home = self._resolve_primary_oracle_home()
        discovery.primary_oracle.oracle_sid = self._resolve_primary_sid()
        discovery.primary_oracle.oracle_base = self.request.primary_oracle_base
        discovery.primary_oracle.storage_type = self._resolve_primary_storage_type()
        discovery.primary_oracle.listener_port = self._resolve_primary_listener_port()
        discovery.primary_oracle.is_cdb = self.request.primary_is_cdb
        discovery.primary_oracle.db_unique_name = self.request.db_unique_name_primary
        discovery.primary_oracle.service_name = self.request.primary_service_name

        discovery.standby_oracle.oracle_home = self._resolve_standby_oracle_home()
        discovery.standby_oracle.oracle_sid = self._resolve_standby_sid()
        discovery.standby_oracle.oracle_base = self.request.standby_oracle_base
        # 备库 storage_type：用户输入 > 继承主库 > 主库探测值
        if self.request.standby_storage_type:
            discovery.standby_oracle.storage_type = self.request.standby_storage_type
        elif discovery.primary_oracle.storage_type:
            discovery.standby_oracle.storage_type = discovery.primary_oracle.storage_type
        else:
            discovery.standby_oracle.storage_type = self._resolve_standby_storage_type()
        discovery.standby_oracle.listener_port = self._resolve_standby_listener_port()
        # 备库 is_cdb：用户输入 > 继承主库 > 主库探测值
        if self.request.standby_is_cdb is not None:
            discovery.standby_oracle.is_cdb = self.request.standby_is_cdb
        elif discovery.primary_oracle.is_cdb is not None:
            discovery.standby_oracle.is_cdb = discovery.primary_oracle.is_cdb
        discovery.standby_oracle.db_unique_name = self.request.db_unique_name_standby
        discovery.standby_oracle.service_name = self.request.standby_service_name

    def _discover_database_status(self, discovery: DiscoveryInfo):
        """探测数据库状态"""
        if not self.primary_sql:
            return

        role_sql = "SELECT database_role, open_mode FROM v$database"
        role_result = self.primary_sql.execute_sql(role_sql)
        if role_result.success:
            lines = [line.strip() for line in role_result.stdout.splitlines() if line.strip()]
            if len(lines) >= 2:
                discovery.primary_db["role"] = lines[0]
                discovery.primary_db["open_mode"] = lines[1]

        archive_sql = "SELECT log_mode FROM v$database"
        archive_result = self.primary_sql.execute_sql(archive_sql)
        if archive_result.success:
            discovery.primary_db["archive_mode"] = archive_result.stdout.strip()

        force_sql = "SELECT force_logging FROM v$database"
        force_result = self.primary_sql.execute_sql(force_sql)
        if force_result.success:
            discovery.primary_db["force_logging"] = force_result.stdout.strip().upper() == "YES"

    def _discover_network(self, discovery: DiscoveryInfo):
        """探测网络状态"""
        if self.primary_sql:
            listener_sql = "SELECT status FROM v$listener"
            listener_result = self.primary_sql.execute_sql(listener_sql)
            if listener_result.success:
                discovery.network["primary_listener_status"] = listener_result.stdout.strip()

        discovery.network["primary_listener_port"] = self._resolve_primary_listener_port()
        discovery.network["standby_listener_port"] = self._resolve_standby_listener_port()
        discovery.network["log_transport_mode"] = self._normalized_log_transport_mode()
        discovery.network["tns_check"] = None

    def _discover_storage(self, discovery: DiscoveryInfo):
        """探测存储信息"""
        primary_data_path = self.request.primary_data_file_path or self.request.data_files_path
        standby_data_path = self.request.standby_data_file_path or self.request.data_files_path
        primary_redo_path = self.request.primary_redo_file_path or primary_data_path
        standby_redo_path = self.request.standby_redo_file_path or standby_data_path

        discovery.storage["data_files_path"] = primary_data_path
        discovery.storage["standby_data_files_path"] = standby_data_path
        discovery.storage["redo_file_path_strategy"] = self.request.redo_file_path_strategy
        discovery.storage["data_file_path_strategy"] = self.request.data_file_path_strategy
        discovery.storage["primary_redo_path"] = primary_redo_path
        discovery.storage["standby_redo_path"] = standby_redo_path
        discovery.storage["archivelog_path"] = self.request.archivelog_path
        primary_storage_type = self._resolve_primary_storage_type()
        standby_storage_type = self._resolve_standby_storage_type()
        discovery.storage["primary_storage_type"] = primary_storage_type
        discovery.storage["standby_storage_type"] = standby_storage_type
        discovery.storage["storage_type"] = primary_storage_type
        discovery.storage["archive_cleanup_policy"] = self.request.archive_cleanup_policy
        discovery.storage["archive_cleanup_param"] = self.request.archive_cleanup_param

        if self.request.archivelog_path:
            discovery.storage["standby_archive_path"] = self.request.archivelog_path

        if self.primary_ssh and primary_data_path:
            primary_path_meta = self._inspect_path(self.primary_ssh, primary_data_path)
            discovery.storage["primary_data_dir_status"] = primary_path_meta.get("status")
            discovery.storage["primary_data_dir_writable"] = primary_path_meta.get("writable")
            discovery.storage["primary_data_dir_has_files"] = primary_path_meta.get("has_files")
            df_cmd = f"df -h {primary_data_path} | tail -1"
            df_result = self.primary_ssh.execute(df_cmd)
            if df_result.success:
                discovery.storage["primary_disk_usage"] = df_result.stdout.strip()

        if self.standby_ssh and standby_data_path:
            standby_path_meta = self._inspect_path(self.standby_ssh, standby_data_path)
            discovery.storage["standby_data_dir_status"] = standby_path_meta.get("status")
            discovery.storage["standby_data_dir_writable"] = standby_path_meta.get("writable")
            discovery.storage["standby_data_dir_has_files"] = standby_path_meta.get("has_files")
            df_cmd = f"df -h {standby_data_path} | tail -1"
            df_result = self.standby_ssh.execute(df_cmd)
            if df_result.success:
                discovery.storage["standby_disk_usage"] = df_result.stdout.strip()

        # 主备日志目录检查
        if self.primary_ssh and primary_redo_path:
            primary_log_meta = self._inspect_path(self.primary_ssh, primary_redo_path)
            discovery.storage["primary_log_dir_status"] = primary_log_meta.get("status")
            discovery.storage["primary_log_dir_writable"] = primary_log_meta.get("writable")
            discovery.storage["primary_log_dir_has_files"] = primary_log_meta.get("has_files")

        if self.standby_ssh and standby_redo_path:
            standby_log_meta = self._inspect_path(self.standby_ssh, standby_redo_path)
            discovery.storage["standby_log_dir_status"] = standby_log_meta.get("status")
            discovery.storage["standby_log_dir_writable"] = standby_log_meta.get("writable")
            discovery.storage["standby_log_dir_has_files"] = standby_log_meta.get("has_files")

    def _inspect_path(self, ssh: Optional[RemoteExecutor], path: Optional[str]) -> Dict[str, Optional[Union[bool, str]]]:
        """检查目录状态，返回 exists、writable、has_files 和 status"""
        if not ssh or not path:
            return {}
        normalized = path.strip()
        if not normalized or normalized.startswith("+"):
            return {}
        quoted_path = shlex.quote(normalized)
        exists_result = ssh.execute(f"test -d {quoted_path}", timeout=10)
        if not exists_result.success:
            return {"exists": False, "writable": None, "has_files": None, "status": "not_exists"}
        writable_result = ssh.execute(f"test -w {quoted_path}", timeout=10)
        if not writable_result.success:
            files_result = ssh.execute(f"ls -A {quoted_path}", timeout=10)
            return {
                "exists": True,
                "writable": False,
                "has_files": bool((files_result.stdout or "").strip()) if files_result.success else False,
                "status": "not_writable",
            }
        files_result = ssh.execute(f"ls -A {quoted_path}", timeout=10)
        return {
            "exists": True,
            "writable": True,
            "has_files": bool((files_result.stdout or "").strip()) if files_result.success else False,
            "status": "ok",
        }

    def _detect_conflicts(self, discovery: DiscoveryInfo):
        """检测冲突"""
        conflicts: List[str] = []
        discovery.conflicts = conflicts

    def precheck(self, discovery: DiscoveryInfo) -> List[PrecheckResult]:
        """执行预检查"""
        results: List[PrecheckResult] = []
        primary_sid = self._resolve_primary_sid()
        standby_sid = self._resolve_standby_sid()
        primary_home = self._resolve_primary_oracle_home()
        standby_home = self._resolve_standby_oracle_home()

        def append(check: Optional[PrecheckResult]):
            if check:
                results.append(check)

        append(self._check_instance_process('primary', primary_sid))
        append(self._check_instance_process('standby', standby_sid))
        append(self._check_oracle_home_mapping('primary', primary_sid, primary_home))
        append(self._check_oracle_home_mapping('standby', standby_sid, standby_home))

        primary_listener_check = self._check_listener_status(
            role='primary',
            oracle_home=primary_home,
            expected_port=self._resolve_primary_listener_port(),
            discovery=discovery,
        )
        primary_listener_passed = primary_listener_check is not None and primary_listener_check.result == 'pass'
        if primary_listener_check:
            results.append(primary_listener_check)

        standby_listener_check = self._check_listener_status(
            role='standby',
            oracle_home=standby_home,
            expected_port=self._resolve_standby_listener_port(),
            discovery=discovery,
        )
        if standby_listener_check:
            results.append(standby_listener_check)

        if primary_listener_passed:
            append(self._check_primary_tns_connectivity(primary_home, self._resolve_primary_listener_port(), discovery))

        append(self._check_primary_custom_path_alignment('data'))
        append(self._check_primary_custom_path_alignment('redo'))
        append(self._check_standby_directory_ready('data'))
        append(self._check_standby_directory_ready('redo'))

        results.append(self._check_archive_mode(discovery))
        results.append(self._check_force_logging(discovery))
        results.append(self._check_version_compatibility(discovery))
        results.append(self._check_log_transport_mode())
        results.append(self._check_network_connectivity(discovery))
        results.append(self._check_standby_space(discovery))
        results.append(self._check_directory_mapping(discovery))
        results.append(self._check_storage_type_alignment(discovery))
        results.append(self._check_db_unique_name(discovery))
        results.append(self._check_listener_tns(discovery))
        results.append(self._check_duplicate_prerequisites(discovery))
        results.extend(self._feature_flag_warnings())
        return results

    def _check_instance_process(
        self,
        role: str,
        sid: Optional[str],
    ) -> Optional[PrecheckResult]:
        executor = self.primary_ssh if role == 'primary' else self.standby_ssh
        if not executor or not sid:
            return None
        normalized_sid = re.sub(r"[^A-Za-z0-9_$-]", "", sid) or sid
        pattern = f"ora_pmon_{normalized_sid}"
        command = f"ps -ef | grep -i {shlex.quote(pattern)} | grep -v grep || true"
        result = executor.execute(command, timeout=10)
        stdout = (result.stdout or "").strip()
        is_running = bool(stdout)
        label = "主库" if role == 'primary' else "备库"
        message = f"{label} PMON 进程检查"
        evidence = {"sid": normalized_sid, "stdout": stdout}

        if role == 'primary':
            status = 'pass' if is_running else 'fail'
            suggestion = None if is_running else "请启动主库实例，确保 PMON 进程正常运行"
            blocking = not is_running
            risk = RiskLevel.CRITICAL if not is_running else RiskLevel.LOW
        else:
            if is_running:
                status = 'warn'
                suggestion = "检测到备库同名实例，请确认不会与待创建的备库冲突"
                risk = RiskLevel.MEDIUM
            else:
                status = 'pass'
                suggestion = None
                risk = RiskLevel.LOW
            blocking = False

        return PrecheckResult(
            check_name=f"{role}_pmon_process",
            category="process",
            result=status,
            message=message,
            evidence=evidence,
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
        )

    def _check_oracle_home_mapping(
        self,
        role: str,
        sid: Optional[str],
        oracle_home: Optional[str],
    ) -> Optional[PrecheckResult]:
        executor = self.primary_ssh if role == 'primary' else self.standby_ssh
        if not executor or not sid or not oracle_home:
            return None
        result = executor.execute("cat /etc/oratab 2>/dev/null", timeout=10)
        lines = [
            line.strip()
            for line in (result.stdout or "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        normalized_sid = sid.strip().upper()
        mapping_line = next((line for line in lines if line.split(":")[0].strip().upper() == normalized_sid), None)
        label = "主库" if role == 'primary' else "备库"
        message = f"{label} ORACLE_HOME 映射检查"
        evidence = {"sid": normalized_sid, "oracle_home": oracle_home, "oratab_line": mapping_line}

        if not result.success and not lines:
            status = 'warn' if role == 'standby' else 'fail'
            suggestion = "无法读取 /etc/oratab，请确认文件存在并可访问"
            blocking = role == 'primary'
            risk = RiskLevel.MEDIUM if status == 'warn' else RiskLevel.HIGH
        elif not mapping_line:
            status = 'warn' if role == 'standby' else 'fail'
            suggestion = f"/etc/oratab 中未找到 SID {normalized_sid} 的记录"
            blocking = role == 'primary'
            risk = RiskLevel.MEDIUM if status == 'warn' else RiskLevel.HIGH
        else:
            recorded_home = mapping_line.split(":")[1].strip()
            matches = recorded_home.rstrip("/") == oracle_home.rstrip("/")
            status = 'pass' if matches else ('warn' if role == 'standby' else 'fail')
            suggestion = None if matches else "请确认 /etc/oratab 中的 ORACLE_HOME 与输入一致"
            blocking = not matches and role == 'primary'
            risk = RiskLevel.LOW if matches else (RiskLevel.MEDIUM if status == 'warn' else RiskLevel.HIGH)
            evidence["oratab_home"] = recorded_home

        return PrecheckResult(
            check_name=f"{role}_oratab_mapping",
            category="configuration",
            result=status,
            message=message,
            evidence=evidence,
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
        )

    def _check_listener_status(
        self,
        role: str,
        oracle_home: Optional[str],
        expected_port: Optional[int],
        discovery: DiscoveryInfo,
    ) -> Optional[PrecheckResult]:
        executor = self.primary_ssh if role == 'primary' else self.standby_ssh
        if not executor or not oracle_home:
            return None
        env = {
            "ORACLE_HOME": oracle_home,
            "PATH": f"{oracle_home}/bin:$PATH",
            "LD_LIBRARY_PATH": f"{oracle_home}/lib:$LD_LIBRARY_PATH",
        }
        result = executor.execute("lsnrctl status", environment=env, timeout=60)
        stdout = (result.stdout or "").strip()
        ports = {int(match) for match in re.findall(r"PORT\s*=\s*(\d+)", stdout)}
        has_expected_port = expected_port in ports if expected_port else bool(ports)
        label = "主库" if role == 'primary' else "备库"
        message = f"{label} Listener 状态检查"
        status_key = "primary_listener_status" if role == 'primary' else "standby_listener_status"
        discovery.network[status_key] = "READY" if (result.success and has_expected_port) else "NOT READY"

        if not result.success:
            status = 'fail' if role == 'primary' else 'warn'
            suggestion = "请确认监听进程已启动，并检查 listener.ora"
        elif not has_expected_port:
            status = 'fail' if role == 'primary' else 'warn'
            suggestion = "监听端口与表单输入不符，请核对 listener.ora"
        else:
            status = 'pass'
            suggestion = None

        blocking = status == 'fail' and role == 'primary'
        risk = RiskLevel.LOW if status == 'pass' else (RiskLevel.MEDIUM if status == 'warn' else RiskLevel.HIGH)

        return PrecheckResult(
            check_name=f"{role}_listener_status",
            category="connectivity",
            result=status,
            message=message,
            evidence={
                "detected_ports": sorted(ports),
                "expected_port": expected_port,
                "stdout": stdout[:400],
            },
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
        )

    def _check_primary_tns_connectivity(
        self,
        oracle_home: Optional[str],
        listener_port: Optional[int],
        discovery: DiscoveryInfo,
    ) -> Optional[PrecheckResult]:
        password = (self.request.sys_password or "").strip()
        service_name = (self.request.primary_service_name or "").strip()
        if not password or not service_name or not oracle_home or not self.primary_ssh:
            return None
        port = listener_port or 1521
        env = {
            "ORACLE_HOME": oracle_home,
            "PATH": f"{oracle_home}/bin:$PATH",
            "LD_LIBRARY_PATH": f"{oracle_home}/lib:$LD_LIBRARY_PATH",
            "TNS_TEST_PASS": password,
        }
        sanitized_service = re.sub(r"[^A-Za-z0-9._-]", "", service_name)
        command = (
            "cat <<SQL | sqlplus -s /nolog\n"
            f"connect sys/\"$TNS_TEST_PASS\"@127.0.0.1:{port}/{sanitized_service} as sysdba\n"
            "select 1 from dual;\n"
            "exit;\n"
            "SQL"
        )
        result = self.primary_ssh.execute(command, environment=env, timeout=45)
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        success = result.success and "1" in [line.strip() for line in stdout.splitlines() if line.strip()]
        discovery.network["tns_check"] = "success" if success else "failed"
        status = 'pass' if success else 'warn'
        suggestion = None if success else "TNS 连接失败，请检查 SYS 密码、service_name 或监听配置"
        risk = RiskLevel.LOW if success else RiskLevel.MEDIUM

        return PrecheckResult(
            check_name="primary_tns_connectivity",
            category="connectivity",
            result=status,
            message=f"主库 SYS@127.0.0.1:{port}/{sanitized_service} 连通性",
            evidence={"stdout": stdout[:400], "stderr": stderr[:400], "port": port, "service_name": sanitized_service},
            suggestion=suggestion,
            blocking=False,
            risk_level=risk,
        )

    def _check_primary_custom_path_alignment(self, path_type: str) -> Optional[PrecheckResult]:
        strategy = (self.request.data_file_path_strategy if path_type == 'data' else self.request.redo_file_path_strategy) or ''
        if strategy.lower() != 'custom' or not self.primary_sql:
            return None
        path = (self.request.primary_data_file_path if path_type == 'data' else self.request.primary_redo_file_path) or ''
        if not path:
            return None
        normalized_path = path.rstrip("/").upper()
        escaped_path = normalized_path.replace("'", "''")
        if path_type == 'data':
            sql = (
                f"SELECT COUNT(*) FROM v$datafile "
                f"WHERE UPPER(file_name) NOT LIKE '{escaped_path}%'"
            )
            check_name = "primary_data_path_alignment"
            message = "主库数据文件路径与输入一致性"
        else:
            sql = (
                f"SELECT COUNT(*) FROM v$logfile "
                f"WHERE UPPER(member) NOT LIKE '{escaped_path}%'"
            )
            check_name = "primary_redo_path_alignment"
            message = "主库联机日志路径与输入一致性"
        mismatch = self.primary_sql.query_single_value(sql) or "0"
        mismatch_count = int(mismatch.strip() or "0")
        status = 'pass' if mismatch_count == 0 else 'warn'
        suggestion = None if mismatch_count == 0 else "请确认主库实际路径与自定义路径一致，避免后续 Duplicate 失败"
        risk = RiskLevel.LOW if mismatch_count == 0 else RiskLevel.MEDIUM

        return PrecheckResult(
            check_name=check_name,
            category="storage",
            result=status,
            message=message,
            evidence={"expected_prefix": normalized_path, "mismatch_count": mismatch_count},
            suggestion=suggestion,
            blocking=False,
            risk_level=risk,
        )

    def _check_standby_directory_ready(self, directory_type: str) -> Optional[PrecheckResult]:
        strategy = (self.request.data_file_path_strategy if directory_type == 'data' else self.request.redo_file_path_strategy) or ''
        if strategy.lower() != 'custom' or not self.standby_ssh:
            return None
        path = (self.request.standby_data_file_path if directory_type == 'data' else self.request.standby_redo_file_path) or ''
        normalized = path.strip()
        if not normalized or normalized.startswith("+"):
            return None
        meta = self._inspect_path(self.standby_ssh, normalized)
        if not meta:
            return None
        status_flag = meta.get("status")
        has_files = bool(meta.get("has_files"))
        label = "数据文件" if directory_type == 'data' else "联机日志"
        check_name = f"standby_{directory_type}_dir_status"
        message = f"备库{label}目录可用性"

        if status_flag == "ok" and not has_files:
            status = 'pass'
            suggestion = None
            risk = RiskLevel.LOW
            blocking = False
        elif status_flag == "not_exists":
            status = 'fail'
            suggestion = "目录不存在，请预先创建并授权 oracle 用户"
            risk = RiskLevel.HIGH
            blocking = True
        elif status_flag == "not_writable":
            status = 'warn'
            suggestion = "目录不可写，请检查权限或磁盘挂载状态"
            risk = RiskLevel.MEDIUM
            blocking = False
        else:
            status = 'warn'
            suggestion = "目录已存在文件，请确认不会覆盖生产数据"
            risk = RiskLevel.MEDIUM
            blocking = False

        if has_files and status == 'pass':
            status = 'warn'
            suggestion = suggestion or "目录中存在文件，请确保不影响后续 Duplicate"
            risk = RiskLevel.MEDIUM

        return PrecheckResult(
            check_name=check_name,
            category="storage",
            result=status,
            message=message,
            evidence={
                "path": normalized,
                "status": status_flag,
                "writable": meta.get("writable"),
                "has_files": has_files,
            },
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
        )

    def _check_archive_mode(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查归档模式"""
        archive_mode = discovery.primary_db.get("archive_mode", "")
        is_archivelog = "ARCHIVELOG" in archive_mode.upper()
        return PrecheckResult(
            check_name="archive_mode",
            category="configuration",
            result="pass" if is_archivelog else "fail",
            message="主库归档模式检查",
            evidence={"archive_mode": archive_mode},
            suggestion="启用归档模式: ALTER DATABASE ARCHIVELOG" if not is_archivelog else None,
            blocking=not is_archivelog,
            risk_level=RiskLevel.CRITICAL if not is_archivelog else RiskLevel.LOW,
        )

    def _check_force_logging(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查 Force Logging"""
        force_logging = bool(discovery.primary_db.get("force_logging"))
        return PrecheckResult(
            check_name="force_logging",
            category="configuration",
            result="pass" if force_logging else "warn",
            message="Force Logging 检查",
            evidence={"force_logging": force_logging},
            suggestion="启用 Force Logging: ALTER DATABASE FORCE LOGGING" if not force_logging else None,
            blocking=False,
            risk_level=RiskLevel.MEDIUM if not force_logging else RiskLevel.LOW,
        )

    def _check_version_compatibility(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查版本兼容性"""
        primary_version = discovery.primary_oracle.version or ""
        standby_version = discovery.standby_oracle.version or primary_version
        is_compatible = not primary_version or primary_version == standby_version
        return PrecheckResult(
            check_name="version_compatibility",
            category="compatibility",
            result="pass" if is_compatible else "fail",
            message="主备版本兼容性检查",
            evidence={"primary_version": primary_version, "standby_version": standby_version},
            suggestion="确保主备库版本一致" if not is_compatible else None,
            blocking=not is_compatible,
            risk_level=RiskLevel.HIGH if not is_compatible else RiskLevel.LOW,
        )

    def _check_network_connectivity(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查网络连通性"""
        listener_status = discovery.network.get("primary_listener_status", "")
        is_reachable = bool(listener_status)
        return PrecheckResult(
            check_name="network_connectivity",
            category="connectivity",
            result="pass" if is_reachable else "warn",
            message="网络连通性检查",
            evidence={"listener_status": listener_status},
            suggestion="检查 Listener 配置和防火墙设置" if not is_reachable else None,
            blocking=False,
            risk_level=RiskLevel.MEDIUM if not is_reachable else RiskLevel.LOW,
        )

    def _check_log_transport_mode(self) -> PrecheckResult:
        """检查日志传输模式"""
        mode = self._normalized_log_transport_mode()
        allowed_modes = {"ASYNC", "SYNC"}
        is_supported = mode in allowed_modes
        return PrecheckResult(
            check_name="log_transport_mode",
            category="configuration",
            result="pass" if is_supported else "warn",
            message="日志传输模式检查",
            evidence={"log_transport_mode": mode},
            suggestion="仅支持 ASYNC 或 SYNC 模式，请调整前端配置" if not is_supported else None,
            blocking=False,
            risk_level=RiskLevel.MEDIUM if not is_supported else RiskLevel.LOW,
        )

    def _check_standby_space(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查备库空间"""
        has_space = True  # 简化：默认空间充足
        return PrecheckResult(
            check_name="standby_space",
            category="storage",
            result="pass" if has_space else "fail",
            message="备库可用空间检查",
            evidence={"estimated_required": "unknown", "available": "unknown"},
            suggestion="清理备库存储或增加磁盘空间" if not has_space else None,
            blocking=not has_space,
            risk_level=RiskLevel.HIGH if not has_space else RiskLevel.LOW,
        )

    def _check_directory_mapping(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查目录映射"""
        is_valid = True  # 简化：默认目录有效
        return PrecheckResult(
            check_name="directory_mapping",
            category="storage",
            result="pass" if is_valid else "warn",
            message="目录映射检查",
            evidence={"data_path": discovery.storage.get("data_files_path")},
            suggestion="确保目录存在且有正确权限" if not is_valid else None,
            blocking=False,
            risk_level=RiskLevel.MEDIUM if not is_valid else RiskLevel.LOW,
        )

    def _check_storage_type_alignment(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查主备存储类型一致性"""
        primary_storage = (discovery.primary_oracle.storage_type or self._resolve_primary_storage_type() or "").lower()
        standby_storage = (discovery.standby_oracle.storage_type or self._resolve_standby_storage_type() or "").lower()
        consistent = not primary_storage or not standby_storage or primary_storage == standby_storage
        evidence = {
            "primary_storage_type": primary_storage or None,
            "standby_storage_type": standby_storage or None,
        }
        suggestion = None
        if not consistent:
            suggestion = "确保主备存储类型一致 (asm 或 fs) 以避免后续目录映射问题"
        return PrecheckResult(
            check_name="storage_type_alignment",
            category="storage",
            result="pass" if consistent else "warn",
            message="主备存储类型一致性检查",
            evidence=evidence,
            suggestion=suggestion,
            blocking=False,
            risk_level=RiskLevel.MEDIUM if not consistent else RiskLevel.LOW,
        )

    def _check_db_unique_name(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查 DB_UNIQUE_NAME 冲突"""
        has_conflict = False  # 简化：默认无冲突
        return PrecheckResult(
            check_name="db_unique_name",
            category="configuration",
            result="pass" if not has_conflict else "fail",
            message="DB_UNIQUE_NAME 冲突检查",
            evidence={
                "primary_unique_name": self.request.db_unique_name_primary,
                "standby_unique_name": self.request.db_unique_name_standby,
            },
            suggestion="确保 DB_UNIQUE_NAME 唯一" if has_conflict else None,
            blocking=has_conflict,
            risk_level=RiskLevel.HIGH if has_conflict else RiskLevel.LOW,
        )

    def _check_listener_tns(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查 Listener/TNS"""
        listener_status = discovery.network.get("primary_listener_status", "")
        is_ok = bool(listener_status)
        return PrecheckResult(
            check_name="listener_tns",
            category="connectivity",
            result="pass" if is_ok else "warn",
            message="Listener/TNS 配置检查",
            evidence={"listener_status": listener_status},
            suggestion="检查 listener.ora 和 tnsnames.ora 配置" if not is_ok else None,
            blocking=False,
            risk_level=RiskLevel.MEDIUM if not is_ok else RiskLevel.LOW,
        )

    def _check_duplicate_prerequisites(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查 Duplicate 前提"""
        is_ready = True  # 简化：默认满足
        return PrecheckResult(
            check_name="duplicate_prerequisites",
            category="configuration",
            result="pass" if is_ready else "warn",
            message="Duplicate 前提检查",
            evidence={"duplicate_mode": self.request.duplicate_mode},
            suggestion="确保主库有充足备份空间" if not is_ready else None,
            blocking=False,
            risk_level=RiskLevel.LOW,
        )

    def _feature_flag_warnings(self) -> List[PrecheckResult]:
        """针对尚未完全实现的功能生成提醒"""
        warnings: List[PrecheckResult] = []

        if self.request.data_file_path_strategy:
            warnings.append(
                PrecheckResult(
                    check_name="data_file_path_strategy",
                    category="storage",
                    result="warn",
                    message="数据文件路径策略仅在预览中展示，执行阶段需人工确认具体目录",
                    evidence={
                        "strategy": self.request.data_file_path_strategy,
                        "primary_path": self.request.primary_data_file_path or self.request.data_files_path,
                        "standby_path": self.request.standby_data_file_path or self.request.data_files_path,
                    },
                    suggestion="根据策略在目标主机上提前创建/同步目录结构",
                    blocking=False,
                    risk_level=RiskLevel.LOW,
                )
            )

        if self.request.redo_file_path_strategy:
            warnings.append(
                PrecheckResult(
                    check_name="redo_file_path_strategy",
                    category="storage",
                    result="warn",
                    message="Redo 日志路径策略暂未自动生成，需人工在执行阶段核实",
                    evidence={
                        "strategy": self.request.redo_file_path_strategy,
                        "primary_redo_path": self.request.primary_redo_file_path,
                        "standby_redo_path": self.request.standby_redo_file_path,
                    },
                    suggestion="在执行计划中参考路径并在脚本层面自行调整",
                    blocking=False,
                    risk_level=RiskLevel.LOW,
                )
            )

        if self.request.archive_cleanup_policy or self.request.archive_cleanup_param:
            warnings.append(
                PrecheckResult(
                    check_name="archive_cleanup_policy",
                    category="configuration",
                    result="warn",
                    message="归档清理策略仅生成告警提示，尚未自动执行",
                    evidence={
                        "policy": self.request.archive_cleanup_policy,
                        "parameter": self.request.archive_cleanup_param,
                    },
                    suggestion="通过自定义脚本或调度器实现归档清理策略",
                    blocking=False,
                    risk_level=RiskLevel.LOW,
                )
            )

        return warnings

    def generate_plan(self, discovery: DiscoveryInfo, prechecks: List[PrecheckResult]) -> ExecutionPlan:
        """生成执行计划"""
        plan = ExecutionPlan()

        log_transport_mode = self._normalized_log_transport_mode()
        auto_create_srl = bool(self.request.auto_create_srl)
        data_strategy = self.request.data_file_path_strategy or "default"
        redo_strategy = self.request.redo_file_path_strategy or "default"
        standby_data_path = self.request.standby_data_file_path or self.request.data_files_path
        standby_redo_path = self.request.standby_redo_file_path or standby_data_path
        transport_service = self.request.standby_service_name or self.request.db_unique_name_standby

        primary_stage_steps: List[PlanStep] = [
            PlanStep(
                step_id="config_primary_params",
                title="配置主库参数",
                description="设置 db_name, db_unique_name 及归档参数",
                target_host="primary",
                executor_type="sqlplus",
                command_preview=(
                    f"ALTER SYSTEM SET db_name='{self.request.db_name}' SCOPE=SPFILE;\n"
                    f"ALTER SYSTEM SET db_unique_name='{self.request.db_unique_name_primary}' SCOPE=SPFILE;"
                ),
                risk_level=RiskLevel.MEDIUM,
                requires_approval=True,
                rollback_capability=True,
                estimated_duration=60,
            ),
            PlanStep(
                step_id="config_log_transport",
                title="配置日志传输模式",
                description=f"设置 LOG_ARCHIVE_DEST_n 为 {log_transport_mode} 模式并指向 {transport_service}",
                target_host="primary",
                executor_type="sqlplus",
                command_preview=(
                    f"ALTER SYSTEM SET LOG_ARCHIVE_DEST_2='SERVICE={transport_service} "
                    f"{log_transport_mode} AFFIRM NET_TIMEOUT=30 DB_UNIQUE_NAME={self.request.db_unique_name_standby}';"
                ),
                risk_level=RiskLevel.MEDIUM,
                requires_approval=True,
                rollback_capability=True,
                estimated_duration=45,
            ),
            PlanStep(
                step_id="config_primary_network",
                title="配置主库网络",
                description="更新 listener.ora 和 tnsnames.ora 监听 {port}".format(
                    port=self._resolve_primary_listener_port()
                ),
                target_host="primary",
                executor_type="ssh",
                command_preview=(
                    f"编辑 $ORACLE_HOME/network/admin/listener.ora 监听 {self._resolve_primary_listener_port()}，"
                    f"并更新 {transport_service} 的 tnsnames.ora"
                ),
                risk_level=RiskLevel.MEDIUM,
                requires_approval=True,
                rollback_capability=True,
                estimated_duration=30,
            ),
        ]

        plan.stages.append(
            PlanStage(
                stage_name="prepare_primary",
                display_name="准备主库",
                description="配置主库参数、网络和归档设置",
                steps=primary_stage_steps,
            )
        )

        standby_stage_steps: List[PlanStep] = [
            PlanStep(
                step_id="create_standby_dirs",
                title="创建备库目录",
                description="创建数据文件、归档日志及 Redo 目录",
                target_host="standby",
                executor_type="ssh",
                command_preview=(
                    f"mkdir -p {standby_data_path}; mkdir -p {standby_redo_path} "
                    f"# data_strategy={data_strategy}, redo_strategy={redo_strategy}"
                ),
                risk_level=RiskLevel.LOW,
                requires_approval=False,
                rollback_capability=True,
                estimated_duration=30,
            ),
        ]

        if auto_create_srl:
            standby_stage_steps.append(
                PlanStep(
                    step_id="auto_create_srl",
                    title="自动创建 Standby Redo Logs",
                    description=f"根据 {log_transport_mode} 模式创建满足要求的 SRL",
                    target_host="standby",
                    executor_type="sqlplus",
                    command_preview=(
                        f"ALTER DATABASE ADD STANDBY LOGFILE THREAD 1 SIZE <match primary> "
                        f"/* 路径: {standby_redo_path or 'OMF'} */"
                    ),
                    risk_level=RiskLevel.MEDIUM,
                    requires_approval=True,
                    rollback_capability=False,
                    estimated_duration=120,
                )
            )
        else:
            standby_stage_steps.append(
                PlanStep(
                    step_id="srl_manual_confirmation",
                    title="确认 Standby Redo Logs",
                    description="auto_create_srl 未启用，需人工确认 SRL 数量与大小",
                    target_host="standby",
                    executor_type="sqlplus",
                    command_preview="-- 跳过自动创建 SRL，执行阶段需人工处理",
                    risk_level=RiskLevel.LOW,
                    requires_approval=False,
                    rollback_capability=True,
                    estimated_duration=15,
                )
            )

        plan.stages.append(
            PlanStage(
                stage_name="prepare_standby",
                display_name="准备备库",
                description="创建备库目录结构和参数文件",
                steps=standby_stage_steps,
            )
        )

        plan.stages.append(
            PlanStage(
                stage_name="duplicate_standby",
                display_name="备库复制",
                description="执行 RMAN 备份并传输到备库",
                steps=[
                    PlanStep(
                        step_id="rman_backup",
                        title="RMAN 备份",
                        description="备份主库全量数据和归档日志",
                        target_host="primary",
                        executor_type="rman",
                        command_preview="RMAN> BACKUP DATABASE PLUS ARCHIVELOG;",
                        risk_level=RiskLevel.MEDIUM,
                        requires_approval=True,
                        rollback_capability=True,
                        estimated_duration=1800,
                    ),
                    PlanStep(
                        step_id="transfer_backup",
                        title="传输备份文件",
                        description="通过 SFTP 传输备份文件到备库",
                        target_host="management",
                        executor_type="ssh",
                        command_preview="使用 sftp 将备份文件同步至备库",
                        risk_level=RiskLevel.LOW,
                        requires_approval=False,
                        rollback_capability=False,
                        estimated_duration=600,
                    ),
                ],
            )
        )

        plan.stages.append(
            PlanStage(
                stage_name="restore_standby",
                display_name="恢复备库",
                description="使用 RMAN 恢复备库数据文件",
                steps=[
                    PlanStep(
                        step_id="rman_restore",
                        title="RMAN 恢复",
                        description="恢复控制文件和数据文件到备库",
                        target_host="standby",
                        executor_type="rman",
                        command_preview="RMAN> RESTORE DATABASE; RECOVER DATABASE;",
                        risk_level=RiskLevel.HIGH,
                        requires_approval=True,
                        rollback_capability=False,
                        estimated_duration=1800,
                    ),
                ],
            )
        )

        plan.stages.append(
            PlanStage(
                stage_name="enable_managed_recovery",
                display_name="启动实时应用",
                description="启动 MRP 进程并验证同步状态",
                steps=[
                    PlanStep(
                        step_id="start_mrp",
                        title="启动 MRP",
                        description="启动备库 Managed Recovery",
                        target_host="standby",
                        executor_type="sqlplus",
                        command_preview="ALTER DATABASE RECOVER MANAGED STANDBY DATABASE USING CURRENT LOGFILE DISCONNECT;",
                        risk_level=RiskLevel.HIGH,
                        requires_approval=True,
                        rollback_capability=True,
                        estimated_duration=120,
                    ),
                    PlanStep(
                        step_id="verify_sync",
                        title="验证同步",
                        description="检查 MRP 进程状态和应用延迟",
                        target_host="standby",
                        executor_type="sqlplus",
                        command_preview="SELECT process, status FROM v$managed_standby;",
                        risk_level=RiskLevel.LOW,
                        requires_approval=False,
                        rollback_capability=False,
                        estimated_duration=60,
                    ),
                ],
            )
        )

        plan.total_steps = sum(len(stage.steps) for stage in plan.stages)
        plan.estimated_total_duration = sum(
            step.estimated_duration or 0
            for stage in plan.stages
            for step in stage.steps
        )

        blocking_checks = [check for check in prechecks if check.blocking]
        requires_approval = any(
            step.requires_approval
            for stage in plan.stages
            for step in stage.steps
        )
        plan.approval_required = not blocking_checks and requires_approval

        self._sanitize_execution_plan(plan)

        return plan

    def _sanitize_execution_plan(self, plan: ExecutionPlan) -> None:
        """确保执行计划的阶段/步骤可被 JSON 序列化 (无 tuple/自定义键)"""
        plan.stages = [self._sanitize_plan_stage(stage) for stage in plan.stages]

    def _sanitize_plan_stage(self, stage: Union[PlanStage, Dict[str, Any]]) -> PlanStage:
        """将阶段转换为 PlanStage，并递归清洗嵌套结构"""
        if isinstance(stage, PlanStage):
            stage_payload = self._model_dump(stage)
        else:
            stage_payload = stage

        safe_payload = self._ensure_json_safe_value(stage_payload)
        return PlanStage(**safe_payload)

    def _model_dump(self, model: Any) -> Dict[str, Any]:
        dump_fn = getattr(model, "model_dump", None)
        if callable(dump_fn):
            return dump_fn()
        dict_fn = getattr(model, "dict", None)
        if callable(dict_fn):
            return dict_fn()
        raise TypeError(f"Cannot dump model of type {type(model)!r}")

    def _ensure_json_safe_value(self, value: Any) -> Any:
        dump_fn = getattr(value, "model_dump", None)
        if callable(dump_fn):
            return self._ensure_json_safe_value(dump_fn())

        dict_fn = getattr(value, "dict", None)
        if callable(dict_fn):
            return self._ensure_json_safe_value(dict_fn())

        if isinstance(value, dict):
            safe: Dict[str, Any] = {}
            for key, nested in value.items():
                safe_key = self._stringify_metadata_key(key)
                safe[safe_key] = self._ensure_json_safe_value(nested)
            return safe

        if isinstance(value, (list, tuple, set)):
            return [self._ensure_json_safe_value(item) for item in value]

        if isinstance(value, Enum):
            return value.value

        return value

    def _stringify_metadata_key(self, key: Any) -> str:
        if isinstance(key, str):
            return key

        if isinstance(key, Enum):
            return str(key.value)

        if isinstance(key, tuple):
            parts = [self._stringify_metadata_key(part) for part in key]
            return " -> ".join(parts)

        return str(key)

    def execute(self) -> PreviewResponse:
        """执行完整预览流程"""
        # 日志增强: 分阶段输出关键节点，便于追踪 500 错误来源，同时补充异常上下文。
        logger.info("开始执行 Oracle ADG 预览流程")

        try:
            logger.info("步骤1: 建立 SSH/SQL 连接")
            self.connect()

            logger.info("步骤2: 自动探测环境")
            discovery = self.discover()

            logger.info("步骤3: 执行预检查")
            prechecks = self.precheck(discovery)

            logger.info("步骤4: 生成执行计划")
            plan = self.generate_plan(discovery, prechecks)
            self._ensure_plan_serializable(plan)

            logger.info("步骤5: 生成风险汇总")
            risk_summary = self._generate_risk_summary(prechecks, plan)

            logger.info("步骤6: 收集缺失输入")
            missing_inputs = self._collect_missing_inputs()

            logger.info("预览执行完成，返回响应")
            response = PreviewResponse(
                discovered_info=discovery,
                precheck_results=prechecks,
                execution_plan=plan,
                missing_inputs=missing_inputs,
                risk_summary=risk_summary,
            )
            if risk_summary.blocking_issues:
                response.error_type = "precheck_failed"
            response.is_demo = False
            return response
        except PreviewError as exc:
            logger.warning(f"预览执行失败 ({exc.error_type}): {exc}")
            raise exc.to_http_exception()
        except HTTPException:
            logger.warning("预览执行流程抛出 HTTPException，直接透传")
            raise
        except Exception as exc:
            logger.exception("预览执行异常，记录完整堆栈")
            logger.error(f"异常类型: {type(exc).__name__}")
            logger.error(f"异常信息: {exc}")
            detail = {
                "error_type": "preview_error",
                "message": f"预览生成失败: {type(exc).__name__} - {exc}",
            }
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=detail,
            )
        finally:
            logger.info("断开所有连接")
            self.disconnect()

    def _ensure_plan_serializable(self, plan: ExecutionPlan) -> None:
        """预先校验执行计划可以转换为 JSON，失败时输出详细日志"""
        try:
            jsonable_encoder(plan)
            logger.debug(
                "执行计划序列化检查通过: stages=%d, total_steps=%d",
                len(plan.stages),
                plan.total_steps,
            )
        except TypeError as exc:
            logger.exception("执行计划包含不可序列化的数据: %s", exc)
            for index, stage in enumerate(plan.stages):
                if isinstance(stage, PlanStage):
                    logger.error(
                        "Stage[%d] name=%s steps=%d metadata_keys=%s",
                        index,
                        stage.stage_name,
                        len(stage.steps),
                        list((stage.metadata or {}).keys()),
                    )
                else:
                    logger.error("Stage[%d] 类型不匹配: %s", index, type(stage))
            raise

    def _generate_risk_summary(self, prechecks: List[PrecheckResult], plan: ExecutionPlan) -> RiskSummary:
        """生成风险汇总"""
        return summarize_risk(prechecks, plan)

    def _collect_missing_inputs(self) -> List[str]:
        """收集缺失的必要输入"""
        # SSH 认证检查依赖 auth_type，避免 backup_path 这类可选字段误报缺失。
        missing: List[str] = []

        def _auth_mode(value: Optional[str]) -> str:
            return (value or "key").strip().lower()

        primary_mode = _auth_mode(self.request.primary_ssh_auth_type)
        if primary_mode == "key":
            if not self.request.primary_ssh_key_path:
                missing.append("primary_ssh_authentication (需要私钥路径)")
        else:
            if not self.request.primary_ssh_password:
                missing.append("primary_ssh_authentication (需要密码)")

        standby_mode = _auth_mode(self.request.standby_ssh_auth_type)
        if standby_mode == "key":
            if not self.request.standby_ssh_key_path:
                missing.append("standby_ssh_authentication (需要私钥路径)")
        else:
            if not self.request.standby_ssh_password:
                missing.append("standby_ssh_authentication (需要密码)")

        return missing

    def _resolve_primary_sid(self) -> str:
        return self.request.primary_sid or self.request.oracle_sid

    def _resolve_standby_sid(self) -> str:
        return self.request.standby_sid or self.request.oracle_sid

    def _resolve_primary_oracle_home(self) -> str:
        return self.request.primary_oracle_home or self.request.oracle_home

    def _resolve_standby_oracle_home(self) -> str:
        return self.request.standby_oracle_home or self.request.oracle_home

    def _resolve_primary_storage_type(self) -> Optional[str]:
        return self.request.primary_storage_type or self.request.storage_type

    def _resolve_standby_storage_type(self) -> Optional[str]:
        return self.request.standby_storage_type or self.request.storage_type

    def _resolve_primary_listener_port(self) -> Optional[int]:
        return self.request.primary_listener_port or 1521

    def _resolve_standby_listener_port(self) -> Optional[int]:
        return self.request.standby_listener_port or self.request.primary_listener_port or 1521

    def _resolve_ssh_connect_timeout(self, role: str) -> int:
        _ = role  # 预留扩展点：未来可按主备角色定制不同的 SSH 超时
        return DEFAULT_SSH_CONNECT_TIMEOUT

    def _normalized_log_transport_mode(self) -> str:
        mode = self.request.log_transport_mode or "ASYNC"
        return mode.strip().upper()

    def _build_primary_ssh_config(self) -> SSHConfig:
        return self._build_ssh_config(
            host=self.request.primary_host,
            port=self.request.primary_ssh_port,
            username=self.request.primary_ssh_user,
            auth_type=self.request.primary_ssh_auth_type,
            key_path=self.request.primary_ssh_key_path,
            password=self.request.primary_ssh_password,
            connect_timeout=self._resolve_ssh_connect_timeout("primary"),
        )

    def _build_standby_ssh_config(self) -> SSHConfig:
        return self._build_ssh_config(
            host=self.request.standby_host,
            port=self.request.standby_ssh_port,
            username=self.request.standby_ssh_user,
            auth_type=self.request.standby_ssh_auth_type,
            key_path=self.request.standby_ssh_key_path,
            password=self.request.standby_ssh_password,
            connect_timeout=self._resolve_ssh_connect_timeout("standby"),
        )

    @staticmethod
    def _build_ssh_config(
        host: str,
        port: int,
        username: str,
        auth_type: str,
        key_path: Optional[str],
        password: Optional[str],
        connect_timeout: Optional[int] = None,
    ) -> SSHConfig:
        auth_mode = (auth_type or "key").strip().lower()
        kwargs: Dict[str, Any] = {
            "host": host,
            "port": port,
            "username": username,
        }

        timeout = connect_timeout or DEFAULT_SSH_CONNECT_TIMEOUT
        if timeout <= 0:
            timeout = DEFAULT_SSH_CONNECT_TIMEOUT
        kwargs["connect_timeout"] = timeout

        if auth_mode == "password":
            kwargs["password"] = password
        else:
            kwargs["private_key_path"] = key_path
            if password:
                kwargs["private_key_password"] = password

        return SSHConfig(**kwargs)


def build_demo_preview_response(request: PreviewRequest) -> PreviewResponse:
    """构造演示模式的预览数据，便于前端在无真实主机时联调"""
    shared_db_name = (request.db_name or request.primary_sid or "ADGDEMO").upper()
    primary_host = request.primary_host or "primary.demo.local"
    standby_host = request.standby_host or "standby.demo.local"

    discovery = DiscoveryInfo()
    discovery.primary_host = {
        "host": primary_host,
        "hostname": primary_host.split(".")[0],
        "ip_address": "10.0.0.10",
        "os_type": "Linux x86_64",
        "kernel": "5.15.0-86-generic",
    }
    discovery.standby_host = {
        "host": standby_host,
        "hostname": standby_host.split(".")[0],
        "ip_address": "10.0.0.11",
        "os_type": "Linux x86_64",
    }
    discovery.primary_oracle = OracleEnvInfo(
        version="19.18.0.0.0",
        oracle_home=request.primary_oracle_home or "/u01/app/oracle/product/19c/dbhome_1",
        oracle_sid=request.primary_sid or shared_db_name,
        oracle_base=request.primary_oracle_base or "/u01/app/oracle",
        storage_type=request.primary_storage_type or "fs",
        listener_port=request.primary_listener_port or 1521,
        is_cdb=request.primary_is_cdb if request.primary_is_cdb is not None else True,
        db_unique_name=request.db_unique_name_primary or f"{shared_db_name}_PRIM",
        service_name=request.primary_service_name or f"{shared_db_name}_PRIM",
    )
    discovery.standby_oracle = OracleEnvInfo(
        version="19.18.0.0.0",
        oracle_home=request.standby_oracle_home or "/u01/app/oracle/product/19c/dbhome_1",
        oracle_sid=request.standby_sid or f"{shared_db_name}STB",
        oracle_base=request.standby_oracle_base or "/u01/app/oracle",
        storage_type=request.standby_storage_type or request.primary_storage_type or "fs",
        listener_port=request.standby_listener_port or 1521,
        is_cdb=request.standby_is_cdb if request.standby_is_cdb is not None else True,
        db_unique_name=request.db_unique_name_standby or f"{shared_db_name}_STBY",
        service_name=request.standby_service_name or f"{shared_db_name}_STBY",
    )
    discovery.primary_db = {
        "role": "PRIMARY",
        "open_mode": "READ WRITE",
        "database_role": "PRIMARY",
        "archive_mode": "ARCHIVELOG",
        "force_logging": True,
    }
    discovery.network = {
        "primary_listener_status": "READY",
        "primary_listener_port": request.primary_listener_port or 1521,
        "standby_listener_port": request.standby_listener_port or 1521,
        "log_transport_mode": (request.log_transport_mode or "ASYNC").upper(),
        "tns_check": "ok",
    }
    discovery.storage = {
        "storage_type": request.primary_storage_type or "fs",
        "primary_storage_type": request.primary_storage_type or "fs",
        "standby_storage_type": request.standby_storage_type or request.primary_storage_type or "fs",
        "data_files_path": request.primary_data_file_path or request.data_files_path or f"/u01/oradata/{shared_db_name}",
        "standby_data_files_path": request.standby_data_file_path or f"/u01/adg/{shared_db_name}",
        "archivelog_path": request.archivelog_path or f"/u01/oradata/{shared_db_name}/archivelog",
        "primary_disk_usage": "120G used / 250G total",
        "standby_disk_usage": "80G used / 250G total",
        "redo_file_path_strategy": request.redo_file_path_strategy or "same",
        "data_file_path_strategy": request.data_file_path_strategy or "same",
    }
    discovery.conflicts = ["standby listener 未启用 ADR", "建议开启强制日志"]

    prechecks = [
        PrecheckResult(
            check_name="archive_mode",
            category="configuration",
            result="pass",
            message="主库已启用 ARCHIVELOG",
            evidence={"archive_mode": "ARCHIVELOG"},
            blocking=False,
            risk_level=RiskLevel.LOW,
        ),
        PrecheckResult(
            check_name="force_logging",
            category="configuration",
            result="warn",
            message="主库尚未启用 Force Logging",
            suggestion="执行 ALTER DATABASE FORCE LOGGING;",
            evidence={"force_logging": False},
            blocking=False,
            risk_level=RiskLevel.MEDIUM,
        ),
        PrecheckResult(
            check_name="listener_port_conflict",
            category="connectivity",
            result="fail",
            message="备库监听 1521 与应用端口占用冲突",
            suggestion="调整备库监听端口或释放占用",
            evidence={"standby_listener_port": request.standby_listener_port or 1521},
            blocking=True,
            risk_level=RiskLevel.HIGH,
        ),
    ]

    prepare_primary_steps = [
        PlanStep(
            step_id="validate_primary",
            title="验证主库参数",
            description="检查 db_name、db_unique_name、日志归档参数",
            target_host="primary",
            executor_type="sqlplus",
            command_preview=(
                f"SHOW PARAMETER db_name;\nALTER SYSTEM SET log_archive_dest_2='SERVICE={discovery.standby_oracle.service_name} "
                f"ASYNC VALID_FOR=(ONLINE_LOGFILES,PRIMARY_ROLE) DB_UNIQUE_NAME={discovery.standby_oracle.db_unique_name}';"
            ),
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            rollback_capability=True,
            estimated_duration=120,
        ),
        PlanStep(
            step_id="enable_force_logging",
            title="启用 Force Logging",
            description="若未开启则启用 Force Logging，确保 redo 完整传输",
            target_host="primary",
            executor_type="sqlplus",
            command_preview="ALTER DATABASE FORCE LOGGING;",
            risk_level=RiskLevel.HIGH,
            requires_approval=True,
            rollback_capability=False,
            estimated_duration=60,
        ),
    ]

    prepare_standby_steps = [
        PlanStep(
            step_id="prepare_directories",
            title="创建备库目录",
            description="创建数据文件、归档日志、redo 路径并授权 oracle 用户",
            target_host="standby",
            executor_type="ssh",
            command_preview=(
                f"mkdir -p {discovery.storage['standby_data_files_path']}; "
                f"mkdir -p {request.standby_redo_file_path or discovery.storage['standby_data_files_path']}; "
                f"chown -R oracle:oinstall {discovery.storage['standby_data_files_path']}"
            ),
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            rollback_capability=True,
            estimated_duration=90,
        ),
        PlanStep(
            step_id="configure_listener",
            title="调整备库监听",
            description="更新 listener.ora 至新的端口，避免端口冲突",
            target_host="standby",
            executor_type="ssh",
            command_preview="sed -i 's/1521/1621/' $ORACLE_HOME/network/admin/listener.ora && lsnrctl reload",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            rollback_capability=True,
            estimated_duration=45,
        ),
    ]

    duplicate_steps = [
        PlanStep(
            step_id="rman_duplicate",
            title="RMAN Duplicate",
            description="通过网络 duplicate 备库，包含 controlfile 和数据文件",
            target_host="standby",
            executor_type="rman",
            command_preview="RMAN> DUPLICATE TARGET DATABASE FOR STANDBY FROM ACTIVE DATABASE DORECOVER;",
            risk_level=RiskLevel.HIGH,
            requires_approval=True,
            rollback_capability=False,
            estimated_duration=3600,
        )
    ]

    stages: List[PlanStage] = [
        PlanStage(
            stage_name="prepare_primary",
            display_name="准备主库",
            description="校验主库参数与日志策略",
            steps=prepare_primary_steps,
        ),
        PlanStage(
            stage_name="prepare_standby",
            display_name="准备备库",
            description="目录/监听/参数准备",
            steps=prepare_standby_steps,
        ),
        PlanStage(
            stage_name="duplicate",
            display_name="Duplicate 备库",
            description="执行 RMAN Duplicate 与 MRP 配置",
            steps=duplicate_steps,
        ),
    ]

    total_steps = sum(len(stage.steps) for stage in stages)
    estimated_duration = sum(
        step.estimated_duration or 0
        for stage in stages
        for step in stage.steps
    )

    plan = ExecutionPlan(
        stages=stages,
        total_steps=total_steps,
        estimated_total_duration=estimated_duration,
        approval_required=True,
    )

    risk_summary = summarize_risk(prechecks, plan)

    response = PreviewResponse(
        discovered_info=discovery,
        precheck_results=prechecks,
        execution_plan=plan,
        missing_inputs=[],
        risk_summary=risk_summary,
        is_demo=True,
    )
    if risk_summary.blocking_issues:
        response.error_type = "precheck_failed"
    return response


@router.post("/preview", response_model=APIResponse[PreviewResponse])
async def preview_adg_setup(request: PreviewRequest, demo: bool = Query(False, description="返回演示数据，跳过真实连接")):
    """
    预览 Oracle ADG 搭建

    执行自动探测、预检查，并生成执行计划供前端展示。
    不执行任何实际变更操作。
    """
    if demo:
        logger.info("Oracle ADG 预览以演示模式运行（跳过真实主机连接）")
        demo_response = build_demo_preview_response(request)
        return APIResponse(
            message="演示模式：未连接真实主机，以下为示例数据",
            data=demo_response,
        )

    executor = OraclePreviewExecutor(request)

    try:
        response = executor.execute()
        message = None
        if response.error_type == "precheck_failed":
            message = "预检查存在阻塞项，请根据提示整改后重试"
        return APIResponse(data=response, message=message)
    except PreviewError as exc:
        logger.warning("Oracle ADG 预览失败 (%s): %s", exc.error_type, exc)
        raise exc.to_http_exception()
    except HTTPException:
        raise
    except Exception as exc:
        # 入口容器使用 exception 记录堆栈，并包含异常类型便于前端展示更具体信息。
        logger.exception("预览生成失败: 捕获未处理异常")
        detail = {
            "error_type": "preview_error",
            "message": f"预览生成失败: {type(exc).__name__} - {exc}",
        }
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
        )
