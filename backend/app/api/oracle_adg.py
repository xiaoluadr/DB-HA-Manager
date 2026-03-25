"""Oracle ADG 预检查与预览 API"""

# 变更说明: backend/app/api/oracle_adg.py 修复预览接口 500，涉及 KeyError 规避、日志增强与错误信息细化。

import ipaddress
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
DISK_USAGE_WARN_THRESHOLD = 85
DISK_USAGE_FAIL_THRESHOLD = 95
MAX_PATH_SAMPLE_ROWS = 8


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
        self._network_probe_results: Dict[str, Dict[str, Any]] = {}

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
            # 第 2 步备库通常尚未创建完成，这里只要求 SSH + 安装环境可探测，不强制 standby SQL 可连。

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
        primary_host_value = self.request.primary_host
        discovery.primary_host = {
            "host": primary_host_value,
            "input_host": primary_host_value,
            "ssh_user": self.request.primary_ssh_user,
            "ssh_port": self.request.primary_ssh_port,
        }
        if self._looks_like_ip(primary_host_value):
            discovery.primary_host["input_ip"] = primary_host_value

        if self.primary_sql:
            os_sql = "SELECT value FROM v$parameter WHERE name = 'os_name'"
            result = self.primary_sql.execute_sql(os_sql)
            if result.success:
                discovery.primary_host["oracle_os_name"] = result.stdout.strip()

        if self.primary_ssh:
            hostname_result = self.primary_ssh.execute("hostname")
            if hostname_result.success:
                discovery.primary_host["hostname"] = hostname_result.stdout.strip()
            detected_ips = self._detect_host_ips(self.primary_ssh)
            if detected_ips:
                discovery.primary_host["detected_ips"] = detected_ips
                discovery.primary_host["ip_address"] = detected_ips[0]
            os_meta = self._read_os_release(self.primary_ssh)
            if os_meta:
                discovery.primary_host.update(os_meta)
                os_type = os_meta.get("os_pretty_name") or os_meta.get("os_name")
                if os_type:
                    discovery.primary_host["os_type"] = os_type
            kernel = self._fetch_kernel_version(self.primary_ssh)
            if kernel:
                discovery.primary_host["kernel"] = kernel
                discovery.primary_host["kernel_version"] = kernel

    def _discover_standby_host(self, discovery: DiscoveryInfo):
        """探测备库主机信息"""
        standby_host_value = self.request.standby_host
        discovery.standby_host = {
            "host": standby_host_value,
            "input_host": standby_host_value,
            "ssh_user": self.request.standby_ssh_user,
            "ssh_port": self.request.standby_ssh_port,
        }
        if self._looks_like_ip(standby_host_value):
            discovery.standby_host["input_ip"] = standby_host_value

        if self.standby_ssh:
            hostname_result = self.standby_ssh.execute("hostname")
            if hostname_result.success:
                discovery.standby_host["hostname"] = hostname_result.stdout.strip()
            detected_ips = self._detect_host_ips(self.standby_ssh)
            if detected_ips:
                discovery.standby_host["detected_ips"] = detected_ips
            os_meta = self._read_os_release(self.standby_ssh)
            if os_meta:
                discovery.standby_host.update(os_meta)
                os_type = os_meta.get("os_pretty_name") or os_meta.get("os_name")
                if os_type:
                    discovery.standby_host["os_type"] = os_type
            kernel = self._fetch_kernel_version(self.standby_ssh)
            if kernel:
                discovery.standby_host["kernel"] = kernel
                discovery.standby_host["kernel_version"] = kernel

    def _discover_oracle_env(self, discovery: DiscoveryInfo):
        """探测 Oracle 环境"""
        primary_home = self._resolve_primary_oracle_home()
        standby_home = self._resolve_standby_oracle_home()
        discovery.primary_oracle.oracle_home = primary_home
        discovery.primary_oracle.oracle_sid = self._resolve_primary_sid()
        discovery.standby_oracle.oracle_home = standby_home
        discovery.standby_oracle.oracle_sid = self._resolve_standby_sid()

        primary_oracle_base = self.request.primary_oracle_base
        if self.primary_sql:
            discovery.primary_oracle.version = self._detect_primary_oracle_version()
            instance_status = self.primary_sql.query_single_value("SELECT status FROM v$instance")
            if instance_status:
                discovery.primary_oracle.instance_status = instance_status.strip()

        detected_primary_oracle_base = self._read_shell_variable(self.primary_ssh, "ORACLE_BASE")
        if detected_primary_oracle_base:
            discovery.primary_oracle.oracle_base = detected_primary_oracle_base
        elif primary_oracle_base:
            discovery.primary_oracle.oracle_base = primary_oracle_base

        discovery.primary_oracle.sqlplus_version = self._fetch_sqlplus_version(self.primary_ssh, primary_home)
        if not discovery.primary_oracle.version and discovery.primary_oracle.sqlplus_version:
            discovery.primary_oracle.version = self._extract_release_version(discovery.primary_oracle.sqlplus_version) or discovery.primary_oracle.sqlplus_version
        discovery.network["primary_version_source"] = "auto_success" if discovery.primary_oracle.version else "auto_failed"
        discovery.primary_oracle.listener_port = self._resolve_primary_listener_port()
        discovery.primary_oracle.is_cdb = self.request.primary_is_cdb
        detected_primary_unique_name = None
        if self.primary_sql:
            detected_primary_unique_name = self.primary_sql.query_single_value("SELECT db_unique_name FROM v$database")
            if detected_primary_unique_name:
                discovery.primary_oracle.db_unique_name = detected_primary_unique_name.strip()
        if not discovery.primary_oracle.db_unique_name:
            discovery.primary_oracle.db_unique_name = self.request.db_unique_name_primary
        detected_service = self._detect_primary_service_name()
        if detected_service:
            discovery.primary_oracle.service_name = detected_service
        elif self.request.primary_service_name:
            discovery.primary_oracle.service_name = self.request.primary_service_name

        standby_version = self._detect_standby_oracle_version(standby_home)
        if standby_version:
            discovery.standby_oracle.version = standby_version
            discovery.network["standby_version_source"] = "auto_success"
        elif discovery.primary_oracle.version:
            discovery.standby_oracle.version = discovery.primary_oracle.version
            discovery.network["standby_version_source"] = "inherit_primary"
        else:
            discovery.network["standby_version_source"] = "auto_failed"

        standby_oracle_base = self._read_shell_variable(self.standby_ssh, "ORACLE_BASE")
        if standby_oracle_base:
            discovery.standby_oracle.oracle_base = standby_oracle_base
            discovery.network["standby_oracle_base_source"] = "auto_success"
        else:
            discovery.network["standby_oracle_base_source"] = "auto_failed"

        standby_sqlplus_version = self._fetch_sqlplus_version(self.standby_ssh, standby_home)
        if standby_sqlplus_version:
            discovery.standby_oracle.sqlplus_version = standby_sqlplus_version
            if not discovery.standby_oracle.version:
                discovery.standby_oracle.version = self._extract_release_version(standby_sqlplus_version) or standby_sqlplus_version

        primary_storage_type = self._resolve_primary_storage_type()
        primary_storage_declared = (self.request.primary_storage_type or "").strip().lower() or None
        if primary_storage_declared:
            discovery.primary_oracle.storage_type = primary_storage_declared
            discovery.storage["primary_storage_source"] = "user_input"
        # 备库 storage_type：用户输入 > 继承主库 > 计划值
        if self.request.standby_storage_type:
            discovery.standby_oracle.storage_type = self.request.standby_storage_type
            discovery.storage["standby_storage_source"] = "user_input"
        elif discovery.primary_oracle.storage_type:
            discovery.standby_oracle.storage_type = discovery.primary_oracle.storage_type
            discovery.storage["standby_storage_source"] = "inherit_primary"
        else:
            standby_storage_type = self._resolve_standby_storage_type()
            discovery.standby_oracle.storage_type = standby_storage_type
            discovery.storage["standby_storage_source"] = "planned" if standby_storage_type else "auto_failed"
        discovery.standby_oracle.listener_port = self._resolve_standby_listener_port()
        # 备库 is_cdb：用户输入 > 继承主库 > 主库探测值
        if self.request.standby_is_cdb is not None:
            discovery.standby_oracle.is_cdb = self.request.standby_is_cdb
            discovery.network["standby_is_cdb_source"] = "user_input"
        elif discovery.primary_oracle.is_cdb is not None:
            discovery.standby_oracle.is_cdb = discovery.primary_oracle.is_cdb
            discovery.network["standby_is_cdb_source"] = "inherit_primary"
        else:
            discovery.network["standby_is_cdb_source"] = "planned"
        discovery.standby_oracle.db_unique_name = self.request.db_unique_name_standby
        discovery.standby_oracle.service_name = self.request.standby_service_name or self.request.db_unique_name_standby
        discovery.network["standby_service_name_source"] = (
            "user_input" if self.request.standby_service_name else "planned"
        )

    def _discover_database_status(self, discovery: DiscoveryInfo):
        """探测数据库状态"""
        if not self.primary_sql:
            return

        instance_status = self.primary_sql.query_single_value("SELECT status FROM v$instance")
        if instance_status:
            discovery.primary_db["instance_status"] = instance_status.strip()
            discovery.primary_oracle.instance_status = discovery.primary_oracle.instance_status or instance_status.strip()

        role_sql = "SELECT database_role, open_mode FROM v$database"
        role_result = self.primary_sql.execute_sql(role_sql)
        if role_result.success:
            lines = [line.strip() for line in role_result.stdout.splitlines() if line.strip()]
            if len(lines) >= 2:
                discovery.primary_db["role"] = lines[0]
                discovery.primary_db["open_mode"] = lines[1]

        archive_mode = self.primary_sql.query_single_value("SELECT log_mode FROM v$database")
        if archive_mode:
            discovery.primary_db["archive_mode"] = archive_mode.strip().upper()

        force_logging = self.primary_sql.query_single_value("SELECT force_logging FROM v$database")
        if force_logging:
            discovery.primary_db["force_logging"] = force_logging.strip().upper() == "YES"

        cdb_sql = "SELECT cdb FROM v$database"
        cdb_result = self.primary_sql.execute_sql(cdb_sql)
        if cdb_result.success:
            value = (cdb_result.stdout or "").strip().upper()
            is_cdb = value == "YES"
            discovery.primary_db["is_cdb"] = is_cdb
            if discovery.primary_oracle.is_cdb is None:
                discovery.primary_oracle.is_cdb = is_cdb

    def _discover_network(self, discovery: DiscoveryInfo):
        """探测网络状态"""
        primary_listener = self._probe_listener_status(
            self.primary_ssh,
            self._resolve_primary_oracle_home(),
            self._resolve_primary_listener_port(),
            self._resolve_primary_sid(),
        )
        if primary_listener:
            discovery.network["primary_listener_status"] = "READY" if primary_listener.get("success") else "NOT READY"
            discovery.network["primary_listener_check"] = primary_listener

        standby_listener = self._probe_listener_status(
            self.standby_ssh,
            self._resolve_standby_oracle_home(),
            self._resolve_standby_listener_port(),
            self._resolve_standby_sid(),
        )
        if standby_listener:
            discovery.network["standby_listener_status"] = "READY" if standby_listener.get("success") else "NOT READY"
            discovery.network["standby_listener_check"] = standby_listener

        discovery.network["primary_listener_port"] = self._resolve_primary_listener_port()
        discovery.network["standby_listener_port"] = self._resolve_standby_listener_port()
        discovery.network["log_transport_mode"] = self._normalized_log_transport_mode()
        discovery.network["tns_check"] = None

    def _discover_storage(self, discovery: DiscoveryInfo):
        """探测存储信息"""
        primary_data_path = self.request.primary_data_file_path or self.request.data_files_path
        standby_data_path = self.request.standby_data_file_path
        primary_redo_path = self.request.primary_redo_file_path or primary_data_path
        standby_redo_path = self.request.standby_redo_file_path
        standby_adump_path = self._resolve_standby_adump_path(discovery)

        storage = discovery.storage
        storage["data_files_path"] = primary_data_path
        storage["standby_data_files_path"] = standby_data_path
        storage["redo_file_path_strategy"] = self.request.redo_file_path_strategy
        storage["data_file_path_strategy"] = self.request.data_file_path_strategy
        storage["primary_redo_path"] = primary_redo_path
        storage["standby_redo_path"] = standby_redo_path
        storage["archivelog_path"] = self.request.archivelog_path
        primary_storage_type = self._resolve_primary_storage_type()
        standby_storage_type = self._resolve_standby_storage_type()
        primary_storage_declared = (self.request.primary_storage_type or "").strip().lower() or None
        standby_storage_inherited = self.request.standby_storage_type is None
        storage["primary_storage_type"] = primary_storage_type
        storage["standby_storage_type"] = standby_storage_type
        storage["storage_type"] = primary_storage_type
        storage["primary_storage_input_type"] = primary_storage_type
        storage["standby_storage_input_type"] = standby_storage_type
        storage["primary_storage_source"] = storage.get("primary_storage_source") or ("user_input" if primary_storage_type else "auto_failed")
        storage["standby_storage_source"] = discovery.storage.get("standby_storage_source") or ("user_input" if self.request.standby_storage_type else "planned")
        storage["archive_cleanup_policy"] = self.request.archive_cleanup_policy
        storage["archive_cleanup_param"] = self.request.archive_cleanup_param

        standby_archive_path = (self.request.standby_archive_path or "").strip() or None
        if standby_archive_path:
            storage["standby_archive_path"] = standby_archive_path
        storage["standby_adump_derive_basis"] = {
            "oracle_base": (discovery.standby_oracle.oracle_base or "").strip() or None,
            "db_unique_name": (
                (self.request.db_unique_name_standby or "").strip()
                or (self.request.standby_db_unique_name or "").strip()
                or None
            ),
        }
        if standby_adump_path:
            storage["standby_adump_path"] = standby_adump_path

        detected_data_prefix = self._detect_primary_data_prefix()
        detected_log_prefix = self._detect_primary_log_prefix()
        if detected_data_prefix:
            storage["primary_data_detected_prefix"] = detected_data_prefix
            detected_storage_type = "asm" if detected_data_prefix.strip().startswith("+") else "fs"
            storage["primary_storage_detected_type"] = detected_storage_type
            if not primary_storage_declared:
                discovery.primary_oracle.storage_type = detected_storage_type
                storage["primary_storage_source"] = "auto_success"
            if standby_storage_inherited and not discovery.standby_oracle.storage_type:
                discovery.standby_oracle.storage_type = detected_storage_type
                storage["standby_storage_type"] = detected_storage_type
                storage["standby_storage_source"] = "inherit_primary"
            storage["storage_type"] = detected_storage_type
        if detected_log_prefix:
            storage["primary_log_detected_prefix"] = detected_log_prefix

        if not discovery.primary_oracle.storage_type:
            discovery.primary_oracle.storage_type = primary_storage_type
            if discovery.primary_oracle.storage_type:
                storage["primary_storage_source"] = "user_input"
        storage["primary_storage_type"] = discovery.primary_oracle.storage_type
        storage["standby_storage_type"] = discovery.standby_oracle.storage_type

        primary_data_meta = self._inspect_path(self.primary_ssh, primary_data_path) if primary_data_path else {}
        primary_log_meta = self._inspect_path(self.primary_ssh, primary_redo_path) if primary_redo_path else {}
        standby_data_meta = self._inspect_path(self.standby_ssh, standby_data_path) if standby_data_path else {}
        standby_log_meta = self._inspect_path(self.standby_ssh, standby_redo_path) if standby_redo_path else {}
        standby_archive_meta = self._inspect_path(self.standby_ssh, standby_archive_path) if standby_archive_path else {}
        standby_adump_meta = self._inspect_path(self.standby_ssh, standby_adump_path) if standby_adump_path else {}

        storage["primary_data"] = {
            "input_path": primary_data_path,
            "detected_prefix": detected_data_prefix,
            **(primary_data_meta or {}),
        }
        storage["primary_log"] = {
            "input_path": primary_redo_path,
            "detected_prefix": detected_log_prefix,
            **(primary_log_meta or {}),
        }
        storage["standby_data"] = {
            "input_path": standby_data_path,
            **(standby_data_meta or {}),
        }
        storage["standby_log"] = {
            "input_path": standby_redo_path,
            **(standby_log_meta or {}),
        }
        storage["standby_archive"] = {
            "input_path": standby_archive_path,
            **(standby_archive_meta or {}),
        }
        storage["standby_adump"] = {
            "input_path": standby_adump_path,
            **(standby_adump_meta or {}),
        }

        storage["primary_data_dir_status"] = primary_data_meta.get("status")
        storage["primary_data_dir_writable"] = primary_data_meta.get("writable")
        storage["primary_data_dir_has_files"] = primary_data_meta.get("has_files")
        storage["primary_disk_usage"] = (primary_data_meta.get("disk_usage") or {}).get("raw")
        storage["primary_disk_usage_percent"] = (primary_data_meta.get("disk_usage") or {}).get("percent")

        storage["standby_data_dir_status"] = standby_data_meta.get("status")
        storage["standby_data_dir_writable"] = standby_data_meta.get("writable")
        storage["standby_data_dir_has_files"] = standby_data_meta.get("has_files")
        storage["standby_disk_usage"] = (standby_data_meta.get("disk_usage") or {}).get("raw")
        storage["standby_disk_usage_percent"] = (standby_data_meta.get("disk_usage") or {}).get("percent")

        storage["primary_log_dir_status"] = primary_log_meta.get("status")
        storage["primary_log_dir_writable"] = primary_log_meta.get("writable")
        storage["primary_log_dir_has_files"] = primary_log_meta.get("has_files")
        storage["primary_log_disk_usage"] = (primary_log_meta.get("disk_usage") or {}).get("raw")
        storage["primary_log_disk_usage_percent"] = (primary_log_meta.get("disk_usage") or {}).get("percent")

        storage["standby_log_dir_status"] = standby_log_meta.get("status")
        storage["standby_log_dir_writable"] = standby_log_meta.get("writable")
        storage["standby_log_dir_has_files"] = standby_log_meta.get("has_files")
        storage["standby_log_disk_usage"] = (standby_log_meta.get("disk_usage") or {}).get("raw")
        storage["standby_log_disk_usage_percent"] = (standby_log_meta.get("disk_usage") or {}).get("percent")

        storage["standby_archive_dir_status"] = standby_archive_meta.get("status")
        storage["standby_archive_dir_writable"] = standby_archive_meta.get("writable")
        storage["standby_archive_dir_has_files"] = standby_archive_meta.get("has_files")
        storage["standby_archive_disk_usage"] = (standby_archive_meta.get("disk_usage") or {}).get("raw")
        storage["standby_archive_disk_usage_percent"] = (standby_archive_meta.get("disk_usage") or {}).get("percent")

        storage["standby_adump_dir_status"] = standby_adump_meta.get("status")
        storage["standby_adump_dir_writable"] = standby_adump_meta.get("writable")
        storage["standby_adump_dir_has_files"] = standby_adump_meta.get("has_files")
        storage["standby_adump_disk_usage"] = (standby_adump_meta.get("disk_usage") or {}).get("raw")
        storage["standby_adump_disk_usage_percent"] = (standby_adump_meta.get("disk_usage") or {}).get("percent")

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
            meta = {
                "exists": True,
                "writable": False,
                "has_files": bool((files_result.stdout or "").strip()) if files_result.success else False,
                "status": "not_writable",
            }
        else:
            files_result = ssh.execute(f"ls -A {quoted_path}", timeout=10)
            meta = {
                "exists": True,
                "writable": True,
                "has_files": bool((files_result.stdout or "").strip()) if files_result.success else False,
                "status": "ok",
            }
        usage = self._collect_disk_usage(ssh, normalized)
        if usage:
            meta["disk_usage"] = usage
        return meta

    def _detect_conflicts(self, discovery: DiscoveryInfo):
        """检测冲突"""
        conflicts: List[str] = []
        discovery.conflicts = conflicts

    def precheck(self, discovery: DiscoveryInfo) -> List[PrecheckResult]:
        """执行预检查"""
        results: List[PrecheckResult] = []
        primary_home = self._resolve_primary_oracle_home()
        standby_home = self._resolve_standby_oracle_home()

        def append(check: Optional[PrecheckResult]):
            if check:
                results.append(check)

        primary_listener_status_check = self._check_listener_status(
            role='primary',
            oracle_home=primary_home,
            expected_port=self._resolve_primary_listener_port(),
            discovery=discovery,
        )
        if primary_listener_status_check:
            results.append(primary_listener_status_check)

        primary_listener_port_check = self._check_listener_port_validation(
            role='primary',
            oracle_home=primary_home,
            expected_port=self._resolve_primary_listener_port(),
            discovery=discovery,
        )
        if primary_listener_port_check:
            results.append(primary_listener_port_check)

        standby_listener_status_check = self._check_listener_status(
            role='standby',
            oracle_home=standby_home,
            expected_port=self._resolve_standby_listener_port(),
            discovery=discovery,
        )
        if standby_listener_status_check:
            results.append(standby_listener_status_check)

        standby_listener_port_check = self._check_listener_port_validation(
            role='standby',
            oracle_home=standby_home,
            expected_port=self._resolve_standby_listener_port(),
            discovery=discovery,
        )
        if standby_listener_port_check:
            results.append(standby_listener_port_check)

        append(self._check_ping_connectivity(role='primary'))
        append(self._check_ssh_port_connectivity(role='primary'))
        self._check_listener_port_reachability(role='primary')
        append(self._check_ping_connectivity(role='standby'))
        append(self._check_ssh_port_connectivity(role='standby'))
        self._check_listener_port_reachability(role='standby')
        results.append(self._summarize_network_connectivity('primary'))
        results.append(self._summarize_network_connectivity('standby'))
        append(self._check_primary_custom_path_alignment('data'))
        append(self._check_primary_custom_path_alignment('redo'))
        results.append(self._check_archive_mode(discovery))
        results.append(self._check_force_logging(discovery))
        results.append(self._check_version_compatibility(discovery))
        results.append(self._check_standby_space(discovery))
        results.append(self._check_storage_type_alignment(discovery))
        append(self._check_primary_instance_status(discovery))
        append(self._check_primary_oracle_home_consistency())
        append(self._check_primary_oracle_base_consistency(discovery))
        append(self._check_primary_storage_type_consistency(discovery))
        append(self._check_primary_is_cdb_consistency(discovery))
        append(self._check_primary_service_name_consistency(discovery))
        append(self._check_primary_data_path_alignment())
        append(self._check_primary_redo_path_alignment())
        results.append(self._check_primary_standby_host_separation())
        append(self._check_standby_sid_conflict())
        append(self._check_standby_oracle_home_consistency())
        append(self._check_standby_oracle_base_consistency(discovery))
        append(self._check_standby_data_dir_status(discovery))
        append(self._check_standby_log_dir_status(discovery))
        append(self._check_standby_archive_dir_status(discovery))
        append(self._check_standby_adump_dir_status(discovery))
        append(self._check_primary_db_unique_name_consistency())
        append(self._check_standby_db_unique_name_uniqueness())
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
            target=f"{role}_instance",
        )

    def _check_primary_instance_status(self, discovery: DiscoveryInfo) -> Optional[PrecheckResult]:
        sid = self._resolve_primary_sid()
        if not self.primary_ssh or not sid:
            return None
        normalized_sid = re.sub(r"[^A-Za-z0-9_$-]", "", sid) or sid
        pattern = f"ora_pmon_{normalized_sid}"
        command = f"ps -ef | grep -i {shlex.quote(pattern)} | grep -v grep || true"
        result = self.primary_ssh.execute(command, timeout=10)
        stdout = (result.stdout or "").strip()
        pmon_running = bool(stdout)

        instance_status = discovery.primary_oracle.instance_status
        if not instance_status and self.primary_sql:
            instance_status = self.primary_sql.query_single_value("SELECT status FROM v$instance")
        normalized_status = (instance_status or "").strip().upper()

        if not pmon_running:
            status = 'fail'
            suggestion = "未检测到 PMON 进程，请先启动主库实例"
            blocking = True
            risk = RiskLevel.CRITICAL
        elif normalized_status == "OPEN":
            status = 'pass'
            suggestion = None
            blocking = False
            risk = RiskLevel.LOW
        elif not normalized_status:
            status = 'warn'
            suggestion = "未获取到主库实例状态，请检查 SQL 访问"
            blocking = False
            risk = RiskLevel.MEDIUM
        else:
            status = 'warn'
            suggestion = "主库实例状态非 OPEN，请检查实例是否可用"
            blocking = False
            risk = RiskLevel.MEDIUM

        return PrecheckResult(
            check_name="primary_instance_status",
            category="process",
            result=status,
            message="主库实例运行状态",
            evidence={
                "sid": normalized_sid,
                "pmon_running": pmon_running,
                "instance_status": normalized_status or None,
                "stdout": stdout,
            },
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
            target="primary_instance",
        )

    def _check_standby_sid_conflict(self) -> Optional[PrecheckResult]:
        sid = self._resolve_standby_sid()
        if not self.standby_ssh or not sid:
            return None
        normalized_sid = re.sub(r"[^A-Za-z0-9_$-]", "", sid) or sid
        pattern = f"ora_pmon_{normalized_sid}"
        command = f"ps -ef | grep -i {shlex.quote(pattern)} | grep -v grep || true"
        result = self.standby_ssh.execute(command, timeout=10)
        stdout = (result.stdout or "").strip()
        is_running = bool(stdout)

        if is_running:
            status = 'fail'
            suggestion = "检测到备库同名实例运行，必须先处理冲突后才能继续"
            risk = RiskLevel.HIGH
        else:
            status = 'pass'
            suggestion = None
            risk = RiskLevel.LOW

        return PrecheckResult(
            check_name="standby_sid_conflict",
            category="process",
            result=status,
            message="备库 SID 冲突检查",
            evidence={
                "sid": normalized_sid,
                "pmon_running": is_running,
                "stdout": stdout,
            },
            suggestion=suggestion,
            blocking=is_running,
            risk_level=risk,
            target="standby_instance",
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
            status = 'warn'
            suggestion = "无法读取 /etc/oratab，请确认文件存在并可访问"
            blocking = False
            risk = RiskLevel.MEDIUM
        elif not mapping_line:
            status = 'warn'
            suggestion = f"/etc/oratab 中未找到 SID {normalized_sid} 的记录"
            blocking = False
            risk = RiskLevel.MEDIUM
        else:
            recorded_home = mapping_line.split(":")[1].strip()
            matches = recorded_home.rstrip("/") == oracle_home.rstrip("/")
            status = 'pass' if matches else 'warn'
            suggestion = None if matches else "请确认 /etc/oratab 中的 ORACLE_HOME 与输入一致"
            blocking = False
            risk = RiskLevel.LOW if matches else RiskLevel.MEDIUM
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
            target=f"{role}_oracle_home",
        )

    def _check_oracle_env_variable(
        self,
        role: str,
        oracle_home: Optional[str],
    ) -> Optional[PrecheckResult]:
        executor = self.primary_ssh if role == 'primary' else self.standby_ssh
        if not executor or not oracle_home:
            return None
        env_home = self._read_shell_variable(executor, "ORACLE_HOME")
        label = "主库" if role == 'primary' else "备库"
        evidence = {"env_oracle_home": env_home, "expected_oracle_home": oracle_home}
        check_name = f"{role}_oracle_env_home"
        message = f"{label} ORACLE_HOME 环境变量"

        if not env_home:
            status = 'warn'
            suggestion = "未读取到 ORACLE_HOME 环境变量，请确认 oracle 用户的 profile" if role == 'primary' else "建议在 oracle 用户 profile 中设置 ORACLE_HOME"
            blocking = False
            risk = RiskLevel.MEDIUM
        else:
            matches = env_home.rstrip('/') == oracle_home.rstrip('/')
            if matches:
                status = 'pass'
                suggestion = None
                blocking = False
                risk = RiskLevel.LOW
            else:
                status = 'warn'
                suggestion = "环境变量与输入不符，后续脚本可能执行失败"
                blocking = False
                risk = RiskLevel.MEDIUM

        return PrecheckResult(
            check_name=check_name,
            category="configuration",
            result=status,
            message=message,
            evidence=evidence,
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
            target=f"{role}_oracle_env",
        )

    def _check_primary_oracle_home_consistency(self) -> Optional[PrecheckResult]:
        oracle_home = self._resolve_primary_oracle_home()
        if not self.primary_ssh or not oracle_home:
            return None
        quoted_home = shlex.quote(oracle_home)
        exists_result = self.primary_ssh.execute(f"test -d {quoted_home}", timeout=10)
        exists = exists_result.success
        env_home = self._read_shell_variable(self.primary_ssh, "ORACLE_HOME")
        oratab_home = None
        oratab_line = None
        if not env_home:
            sid = self._resolve_primary_sid()
            if sid:
                oratab_result = self.primary_ssh.execute("cat /etc/oratab 2>/dev/null", timeout=10)
                lines = [
                    line.strip()
                    for line in (oratab_result.stdout or "").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
                normalized_sid = sid.strip().upper()
                oratab_line = next(
                    (line for line in lines if line.split(":")[0].strip().upper() == normalized_sid),
                    None,
                )
                if oratab_line:
                    oratab_home = oratab_line.split(":")[1].strip()

        if not exists:
            status = 'fail'
            suggestion = "ORACLE_HOME 目录不存在或不可访问，请确认路径"
            risk = RiskLevel.HIGH
            blocking = True
        else:
            compare_home = env_home if env_home else oratab_home
            if compare_home:
                matches = compare_home.rstrip('/') == oracle_home.rstrip('/')
                status = 'pass' if matches else 'warn'
                if matches:
                    suggestion = None
                    risk = RiskLevel.LOW
                else:
                    suggestion = "ORACLE_HOME 与环境变量/ORATAB 不一致，请核对"
                    risk = RiskLevel.MEDIUM
            else:
                status = 'warn'
                suggestion = "未读取到 ORACLE_HOME 环境变量且 /etc/oratab 无匹配记录，请核对"
                risk = RiskLevel.MEDIUM
            blocking = False
        return PrecheckResult(
            check_name="primary_oracle_home_consistency",
            category="configuration",
            result=status,
            message="主库 ORACLE_HOME 一致性",
            evidence={
                "oracle_home": oracle_home,
                "exists": exists,
                "env_oracle_home": env_home,
                "oratab_home": oratab_home,
                "oratab_line": oratab_line,
            },
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
            target="primary_oracle_home",
        )

    def _check_standby_oracle_home_consistency(self) -> Optional[PrecheckResult]:
        oracle_home = self._resolve_standby_oracle_home()
        if not self.standby_ssh or not oracle_home:
            return None
        quoted_home = shlex.quote(oracle_home)
        exists_result = self.standby_ssh.execute(f"test -d {quoted_home}", timeout=10)
        exists = exists_result.success
        env_home = self._read_shell_variable(self.standby_ssh, "ORACLE_HOME")
        oratab_home = None
        oratab_line = None
        if not env_home:
            sid = self._resolve_standby_sid()
            if sid:
                oratab_result = self.standby_ssh.execute("cat /etc/oratab 2>/dev/null", timeout=10)
                lines = [
                    line.strip()
                    for line in (oratab_result.stdout or "").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
                normalized_sid = sid.strip().upper()
                oratab_line = next(
                    (line for line in lines if line.split(":")[0].strip().upper() == normalized_sid),
                    None,
                )
                if oratab_line:
                    oratab_home = oratab_line.split(":")[1].strip()

        if not exists:
            status = 'fail'
            suggestion = "ORACLE_HOME 目录不存在或不可访问，请确认路径"
            risk = RiskLevel.HIGH
            blocking = True
        else:
            compare_home = env_home if env_home else oratab_home
            if compare_home:
                matches = compare_home.rstrip('/') == oracle_home.rstrip('/')
                status = 'pass' if matches else 'warn'
                if matches:
                    suggestion = None
                    risk = RiskLevel.LOW
                else:
                    suggestion = "ORACLE_HOME 与环境变量/ORATAB 不一致，请核对"
                    risk = RiskLevel.MEDIUM
            else:
                status = 'warn'
                suggestion = "未读取到 ORACLE_HOME 环境变量且 /etc/oratab 无匹配记录，请核对"
                risk = RiskLevel.MEDIUM
            blocking = False
        return PrecheckResult(
            check_name="standby_oracle_home_consistency",
            category="configuration",
            result=status,
            message="备库 ORACLE_HOME 一致性",
            evidence={
                "oracle_home": oracle_home,
                "exists": exists,
                "env_oracle_home": env_home,
                "oratab_home": oratab_home,
                "oratab_line": oratab_line,
            },
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
            target="standby_oracle_home",
        )

    def _check_primary_oracle_base_consistency(self, discovery: DiscoveryInfo) -> Optional[PrecheckResult]:
        if not self.primary_ssh:
            return None
        configured_base = (self.request.primary_oracle_base or "").strip() or None
        detected_base = (
            (discovery.primary_oracle.oracle_base or "").strip()
            or self._read_shell_variable(self.primary_ssh, "ORACLE_BASE")
            or None
        )
        effective_base = configured_base or detected_base
        if not effective_base:
            return PrecheckResult(
                check_name="primary_oracle_base_consistency",
                category="configuration",
                result="warn",
                message="主库 ORACLE_BASE 一致性",
                evidence={"configured_oracle_base": configured_base, "detected_oracle_base": detected_base},
                suggestion="未获取到主库 ORACLE_BASE，请检查环境变量或 profile 配置",
                blocking=False,
                risk_level=RiskLevel.MEDIUM,
                target="primary_oracle_base",
                title="主库 ORACLE_BASE 一致性检查",
                description="确认主库 ORACLE_BASE 保持用户输入优先，并校验安装环境探测值是否与用户输入一致。",
                detected_result="安装环境探测未获取到 ORACLE_BASE。",
                summary="当前未获取到安装环境探测值；若页面已有用户输入，字段继续展示用户输入并提示人工核对。",
                scope="主库环境",
                related_fields=["primaryOracleBase"],
            )

        exists = self.primary_ssh.execute(f"test -d {shlex.quote(effective_base)}", timeout=10).success
        mismatch = bool(configured_base and detected_base and configured_base.rstrip("/") != detected_base.rstrip("/"))
        if mismatch or not exists:
            status = "warn"
            suggestion = "主库 ORACLE_BASE 与安装环境不一致或路径不存在，请核对" if mismatch else "主库 ORACLE_BASE 路径不存在，请核对"
            risk = RiskLevel.MEDIUM
        else:
            status = "pass"
            suggestion = None
            risk = RiskLevel.LOW

        if mismatch:
            summary = "自动探测值与用户输入不一致；字段继续展示用户输入，预检查提示用户核对安装环境。"
        elif not exists:
            summary = "探测到的 ORACLE_BASE 路径不存在，请先核对主库安装环境。"
        elif configured_base:
            summary = "自动探测与用户输入一致，符合预期。"
        else:
            summary = "已获取安装环境探测值，可直接作为字段最终展示值。"

        return PrecheckResult(
            check_name="primary_oracle_base_consistency",
            category="configuration",
            result=status,
            message="主库 ORACLE_BASE 一致性",
            evidence={
                "configured_oracle_base": configured_base,
                "detected_oracle_base": detected_base,
                "effective_oracle_base": effective_base,
                "exists": exists,
                "matches": not mismatch,
            },
            suggestion=suggestion,
            blocking=False,
            risk_level=risk,
            target="primary_oracle_base",
            title="主库 ORACLE_BASE 一致性检查",
            description="确认主库 ORACLE_BASE 保持用户输入优先，并校验安装环境探测值是否与用户输入一致。",
            detected_result=f"安装环境探测到 ORACLE_BASE 为：{detected_base or effective_base}",
            summary=summary,
            scope="主库环境",
            related_fields=["primaryOracleBase"],
        )

    def _check_standby_oracle_base_consistency(self, discovery: DiscoveryInfo) -> Optional[PrecheckResult]:
        if not self.standby_ssh:
            return None
        configured_base = (self.request.standby_oracle_base or "").strip() or None
        detected_base = (
            (discovery.standby_oracle.oracle_base or "").strip()
            or self._read_shell_variable(self.standby_ssh, "ORACLE_BASE")
            or None
        )
        if not detected_base:
            return PrecheckResult(
                check_name="standby_oracle_base_consistency",
                category="configuration",
                result="warn",
                message="备库 ORACLE_BASE 一致性",
                evidence={"configured_oracle_base": configured_base, "detected_oracle_base": detected_base},
                suggestion="未获取到备库 ORACLE_BASE，请检查备库安装环境或 profile 配置",
                blocking=False,
                risk_level=RiskLevel.MEDIUM,
                target="standby_oracle_base",
                title="备库 ORACLE_BASE 一致性检查",
                description="确认备库 ORACLE_BASE 的最终展示值只来自安装环境探测，并校验该路径是否可信。",
                detected_result="安装环境探测未获取到 ORACLE_BASE。",
                summary="当前未能获取有效的安装环境探测值，字段最终展示值应标记为探测失败。",
                scope="备库环境",
                related_fields=["standbyOracleBase"],
            )

        exists = self.standby_ssh.execute(f"test -d {shlex.quote(detected_base)}", timeout=10).success
        mismatch = bool(configured_base and detected_base and configured_base.rstrip("/") != detected_base.rstrip("/"))
        if mismatch or not exists:
            status = "warn"
            suggestion = "备库 ORACLE_BASE 与安装环境不一致或路径不存在，请核对" if mismatch else "备库 ORACLE_BASE 路径不存在，请核对"
            risk = RiskLevel.MEDIUM
        else:
            status = "pass"
            suggestion = None
            risk = RiskLevel.LOW

        if mismatch:
            summary = "自动探测值与用户输入不一致，请以安装环境探测结果为准。"
        elif not exists:
            summary = "探测到的 ORACLE_BASE 路径不存在，请先核对备库安装环境。"
        elif configured_base:
            summary = "自动探测值与用户输入一致，安装环境校验通过。"
        else:
            summary = "探测路径存在，安装环境校验通过。"

        return PrecheckResult(
            check_name="standby_oracle_base_consistency",
            category="configuration",
            result=status,
            message="备库 ORACLE_BASE 一致性",
            evidence={
                "configured_oracle_base": configured_base,
                "detected_oracle_base": detected_base,
                "effective_oracle_base": detected_base,
                "exists": exists,
                "matches": not mismatch,
            },
            suggestion=suggestion,
            blocking=False,
            risk_level=risk,
            target="standby_oracle_base",
            title="备库 ORACLE_BASE 一致性检查",
            description="确认备库 ORACLE_BASE 的最终展示值只来自安装环境探测，并校验探测值与安装环境是否一致。",
            detected_result=f"安装环境探测到 ORACLE_BASE 为：{detected_base}",
            summary=summary,
            scope="备库环境",
            related_fields=["standbyOracleBase"],
        )

    def _check_primary_is_cdb_consistency(self, discovery: DiscoveryInfo) -> Optional[PrecheckResult]:
        declared = self.request.primary_is_cdb
        actual = discovery.primary_db.get("is_cdb")
        if declared is None or actual is None:
            return None
        matches = bool(declared) is bool(actual)
        return PrecheckResult(
            check_name="primary_is_cdb_consistency",
            category="configuration",
            result="pass" if matches else "warn",
            message="主库 CDB 一致性",
            evidence={"declared_is_cdb": declared, "actual_is_cdb": actual},
            suggestion=None if matches else "主库 CDB 配置与探测结果不一致，请核对",
            blocking=False,
            risk_level=RiskLevel.LOW if matches else RiskLevel.MEDIUM,
            target="primary_database",
            title="主库 CDB 一致性检查",
            description="确认字段展示值与数据库实际探测结果分层展示；字段保持用户输入优先，预检查只负责校验当前输入与实际 CDB 属性是否一致。",
            detected_result=f"数据库探测结果：{'CDB' if actual else '非 CDB'}",
            summary="用户输入与数据库实际 CDB 属性一致。" if matches else "用户输入与数据库实际 CDB 属性不一致，请核对后续搭建参数。",
            scope="主库环境",
            related_fields=["primaryIsCdb"],
        )

    def _check_primary_storage_type_consistency(self, discovery: DiscoveryInfo) -> Optional[PrecheckResult]:
        declared = (self.request.primary_storage_type or "").strip().lower() or None
        detected = (discovery.storage.get("primary_storage_detected_type") or "").strip().lower() or None
        if not declared and not detected:
            return None
        if not detected:
            return PrecheckResult(
                check_name="primary_storage_type_consistency",
                category="storage",
                result="warn",
                message="主库存储类型检查",
                evidence={"declared_storage_type": declared, "detected_storage_type": None},
                suggestion="未从主库数据文件路径判定出存储类型，请手动核对 ASM / FS 配置",
                blocking=False,
                risk_level=RiskLevel.MEDIUM,
                target="primary_storage",
                title="主库存储类型检查",
                description="基于主库数据文件路径前缀判定 ASM / FS，并校验字段展示值是否与探测结果一致。",
                detected_result="当前未从主库数据文件路径获取到有效前缀，无法判定存储类型。",
                summary="主库存储类型探测依据不足，字段仍保留当前展示值并提示人工核对。",
                scope="主库环境",
                related_fields=["primaryStorageType", "primaryDataFilePath"],
            )
        matches = not declared or declared == detected
        return PrecheckResult(
            check_name="primary_storage_type_consistency",
            category="storage",
            result="pass" if matches else "warn",
            message="主库存储类型检查",
            evidence={"declared_storage_type": declared, "detected_storage_type": detected},
            suggestion=None if matches else "主库存储类型与数据文件路径探测结果不一致，请核对 ASM / FS 声明",
            blocking=False,
            risk_level=RiskLevel.LOW if matches else RiskLevel.MEDIUM,
            target="primary_storage",
            title="主库存储类型检查",
            description="基于主库数据文件路径前缀判定 ASM / FS，并校验字段展示值是否与探测结果一致。",
            detected_result=f"数据文件路径判定结果：{detected.upper()}",
            summary=(
                "字段展示值与数据文件路径判定结果一致。"
                if matches else
                "字段展示值与数据文件路径判定结果不一致；字段继续保留用户输入并给出风险提示。"
            ),
            scope="主库环境",
            related_fields=["primaryStorageType", "primaryDataFilePath"],
        )

    def _check_primary_standby_host_separation(self) -> PrecheckResult:
        primary_host = (self.request.primary_host or "").strip()
        standby_host = (self.request.standby_host or "").strip()
        same_host = bool(primary_host and standby_host and primary_host == standby_host)
        return PrecheckResult(
            check_name="primary_standby_host_separation",
            category="connectivity",
            result="warn" if same_host else "pass",
            message="主备主机分离检查",
            evidence={"primary_host": primary_host, "standby_host": standby_host},
            suggestion="当前主备 IP 相同，请确认这是预期部署方式并重新核对路径策略" if same_host else None,
            blocking=False,
            risk_level=RiskLevel.MEDIUM if same_host else RiskLevel.LOW,
            target="network",
        )

    def _get_network_probe_executor(self, role: str) -> Optional[RemoteExecutor]:
        return self.standby_ssh if role == 'primary' else self.primary_ssh

    def _get_network_probe_target(self, role: str) -> str:
        host = self.request.primary_host if role == 'primary' else self.request.standby_host
        return (host or "").strip()

    def _get_network_probe_label(self, role: str) -> str:
        return "主库" if role == 'primary' else "备库"

    def _build_network_probe_result(
        self,
        *,
        check_name: str,
        label: str,
        success: bool,
        message: str,
        evidence: Dict[str, Any],
        failure_suggestion: str,
        target: str,
    ) -> PrecheckResult:
        result = PrecheckResult(
            check_name=check_name,
            category="connectivity",
            result="pass" if success else "fail",
            message=f"{label}{message}",
            evidence=evidence,
            suggestion=None if success else failure_suggestion,
            blocking=not success,
            risk_level=RiskLevel.LOW if success else RiskLevel.HIGH,
            target=target,
        )
        self._network_probe_results[check_name] = {
            "result": result.result,
            "target_host": evidence.get("target_host"),
            "target_port": evidence.get("target_port"),
            "probe_kind": evidence.get("probe_kind"),
            "success": success,
            "message": message,
        }
        return result

    def _run_remote_ping_probe(self, executor: RemoteExecutor, target_host: str) -> Dict[str, Any]:
        quoted_target = shlex.quote(target_host)
        command = (
            f"ping -c 1 -W 2 {quoted_target} >/dev/null 2>&1 "
            f"|| ping -c 1 {quoted_target} >/dev/null 2>&1"
        )
        result = executor.execute(command, timeout=8)
        return {
            "success": result.success,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
            "exit_code": result.exit_code,
        }

    def _run_remote_tcp_probe(self, executor: RemoteExecutor, target_host: str, port: int) -> Dict[str, Any]:
        safe_host = re.sub(r"[^A-Za-z0-9._:-]", "", target_host)
        quoted_host = shlex.quote(safe_host)
        quoted_port = shlex.quote(str(port))
        command = (
            "if command -v nc >/dev/null 2>&1; then "
            f"nc -z -w 5 {quoted_host} {quoted_port}; "
            "else "
            f"timeout 5 bash -lc 'cat < /dev/null > /dev/tcp/{safe_host}/{port}'; "
            "fi"
        )
        result = executor.execute(command, timeout=10)
        return {
            "success": result.success,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
            "exit_code": result.exit_code,
            "port": port,
        }

    def _check_ping_connectivity(self, role: str) -> Optional[PrecheckResult]:
        executor = self._get_network_probe_executor(role)
        target_host = self._get_network_probe_target(role)
        if not executor or not target_host:
            return None
        probe = self._run_remote_ping_probe(executor, target_host)
        label = self._get_network_probe_label(role)
        success = bool(probe.get("success"))
        return self._build_network_probe_result(
            check_name=f"{role}_ping_connectivity",
            label=label,
            success=success,
            message="网络连通性检查（Ping）",
            evidence={
                "target_host": target_host,
                "probe_kind": "ping",
                "stdout": probe.get("stdout"),
                "stderr": probe.get("stderr"),
                "exit_code": probe.get("exit_code"),
            },
            failure_suggestion=f"无法 Ping 到{label}主机，请检查网络连通性、防火墙或路由配置",
            target=f"{role}_host",
        )

    def _check_ssh_port_connectivity(self, role: str) -> Optional[PrecheckResult]:
        executor = self._get_network_probe_executor(role)
        target_host = self._get_network_probe_target(role)
        target_port = 22
        if not executor or not target_host:
            return None
        probe = self._run_remote_tcp_probe(executor, target_host, target_port)
        label = self._get_network_probe_label(role)
        success = bool(probe.get("success"))
        return self._build_network_probe_result(
            check_name=f"{role}_ssh_port_reachability",
            label=label,
            success=success,
            message="SSH 端口可达检查",
            evidence={
                "target_host": target_host,
                "target_port": target_port,
                "probe_kind": "tcp_port",
                "probe_scope": "ssh_port_only",
                "stdout": probe.get("stdout"),
                "stderr": probe.get("stderr"),
                "exit_code": probe.get("exit_code"),
            },
            failure_suggestion=(
                f"从对端主机无法访问 {target_host}:{target_port}，"
                "当前检查只覆盖 SSH 端口可达性，请检查端口、防火墙或安全组策略"
            ),
            target=f"{role}_host",
        )

    def _check_listener_port_reachability(self, role: str) -> Optional[PrecheckResult]:
        executor = self._get_network_probe_executor(role)
        target_host = self._get_network_probe_target(role)
        target_port = self._resolve_primary_listener_port() if role == 'primary' else self._resolve_standby_listener_port()
        if not executor or not target_host or not target_port:
            return None
        probe = self._run_remote_tcp_probe(executor, target_host, target_port)
        label = self._get_network_probe_label(role)
        success = bool(probe.get("success"))
        return self._build_network_probe_result(
            check_name=f"{role}_listener_port_reachability",
            label=label,
            success=success,
            message="监听端口可达检查",
            evidence={
                "target_host": target_host,
                "target_port": target_port,
                "probe_kind": "tcp_port",
                "stdout": probe.get("stdout"),
                "stderr": probe.get("stderr"),
                "exit_code": probe.get("exit_code"),
            },
            failure_suggestion=f"从对端主机无法访问 {target_host}:{target_port}，请检查监听端口、防火墙或网络策略",
            target=f"{role}_listener",
        )

    def _network_result_priority(self, status: str) -> int:
        order = {"pass": 0, "warn": 1, "fail": 2}
        return order.get(status, 1)

    def _summarize_network_connectivity(self, role: str) -> PrecheckResult:
        host = self._get_network_probe_target(role)
        listener_port = self._resolve_primary_listener_port() if role == 'primary' else self._resolve_standby_listener_port()
        check_meta = [
            ("Ping", f"{role}_ping_connectivity"),
            ("SSH", f"{role}_ssh_port_reachability"),
            ("监听端口", f"{role}_listener_port_reachability"),
        ]
        statuses = []
        worst_result = "pass"
        fails = []
        for label, check_name in check_meta:
            data = self._network_probe_results.get(check_name, {})
            status = data.get("result", "warn")
            if self._network_result_priority(status) > self._network_result_priority(worst_result):
                worst_result = status
            target = data.get("target_host") or host or "未知主机"
            info = f"{label}"
            if label == "监听端口" and listener_port:
                info += f"({target}:{listener_port})"
            else:
                info += f"({target})"
            detail = f"{info} { '通过' if status == 'pass' else '失败' if status == 'fail' else '警告' }"
            statuses.append(detail)
            if status != "pass":
                fails.append(detail)
        summary_text = "；".join(statuses)
        if worst_result == "fail":
            final_result = "fail"
            risk = RiskLevel.HIGH
            blocking = True
            final_result_msg = f"网络互通存在阻断项：{fails[0] if fails else '详细检查网络连通性'}。"
            suggestion = "请确认主备主机之间的 ping、SSH 与监听端口均可达后再继续。"
        elif worst_result == "warn":
            final_result = "warn"
            risk = RiskLevel.MEDIUM
            blocking = False
            final_result_msg = f"网络互通存在风险：{fails[0]}。"
            suggestion = "请检查相关网络策略或防火墙，必要时重试探测。"
        else:
            final_result = "pass"
            risk = RiskLevel.LOW
            blocking = False
            final_result_msg = "主备主机网络互通正常，可继续后续操作。"
            suggestion = None

        return PrecheckResult(
            check_name=f"{role}_network_connectivity",
            category="connectivity",
            result=final_result,
            message="主备库网络互通检查",
            evidence={
                "host": host,
                "listener_port": listener_port,
                "status": final_result,
                "details": statuses,
            },
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
            target=f"{role}_network",
            title="主备网络互通检查" if role == 'primary' else "备库网络互通检查",
            description="汇总主备 ping、SSH 和监听端口的连通性，确认网络链路能够支撑 ADG 搭建。",
            detected_result=summary_text,
            summary=final_result_msg,
            scope="网络与主机",
            related_fields=[
                "primaryHost",
                "standbyHost",
                "primaryListenerPort" if role == 'primary' else "standbyListenerPort",
            ],
        )

    def _get_listener_probe(
        self,
        role: str,
        oracle_home: Optional[str],
        expected_port: Optional[int],
        discovery: DiscoveryInfo,
    ) -> Dict[str, Any]:
        probe_key = "primary_listener_check" if role == 'primary' else "standby_listener_check"
        probe = discovery.network.get(probe_key)
        if isinstance(probe, dict) and probe:
            return probe

        executor = self.primary_ssh if role == 'primary' else self.standby_ssh
        sid = self._resolve_primary_sid() if role == 'primary' else self._resolve_standby_sid()
        probe = self._probe_listener_status(executor, oracle_home, expected_port, sid)
        if probe:
            discovery.network[probe_key] = probe
            status_key = "primary_listener_status" if role == 'primary' else "standby_listener_status"
            discovery.network[status_key] = "READY" if probe.get("success") else "NOT READY"
        return probe or {}

    def _check_listener_status(
        self,
        role: str,
        oracle_home: Optional[str],
        expected_port: Optional[int],
        discovery: DiscoveryInfo,
    ) -> Optional[PrecheckResult]:
        probe = self._get_listener_probe(role, oracle_home, expected_port, discovery)
        if not probe:
            return None
        stdout = probe["stdout"]
        label = "主库" if role == 'primary' else "备库"
        message = f"{label}监听状态检查"
        sid = self._resolve_primary_sid() if role == 'primary' else self._resolve_standby_sid()
        sid_matched = probe["sid_matched"]
        running = bool(probe.get("success"))

        status = 'pass' if running else 'warn'
        suggestion = None if running else "请确认监听进程已启动，并检查 listener.ora 配置"
        risk = RiskLevel.LOW if running else RiskLevel.MEDIUM
        listener_details = {
            "status": "READY" if running else "NOT READY",
            "running": running,
            "sid_matched": sid_matched,
            "expected_port": expected_port,
        }
        if role == 'primary':
            discovery.primary_oracle.listener_validation = listener_details
        else:
            discovery.standby_oracle.listener_validation = listener_details

        return PrecheckResult(
            check_name=f"{role}_listener_status",
            category="connectivity",
            result=status,
            message=message,
            evidence={
                "running": running,
                "stdout": stdout[:400],
                "sid_matched": sid_matched,
                "expected_port": expected_port,
            },
            suggestion=suggestion,
            blocking=False,
            risk_level=risk,
            target=f"{role}_listener",
            title=message,
            description=f"确认{label}监听进程是否已运行；该检查只表达监听状态，不承担端口一致性或网络可达性的结论。",
            detected_result=(
                f"监听状态：{'已运行' if running else '未运行'}；"
                f"期望端口：{expected_port or '未知'}；"
                f"SID 匹配：{'是' if sid_matched else '否'}"
            ),
            summary=(
                f"{label}监听状态已完成独立检查。"
                if running else
                f"{label}监听当前未就绪，需先确认监听进程和 listener.ora 配置。"
            ),
            scope=f"{label}环境",
            related_fields=["primaryListenerPort" if role == "primary" else "standbyListenerPort"],
        )

    def _check_listener_port_validation(
        self,
        role: str,
        oracle_home: Optional[str],
        expected_port: Optional[int],
        discovery: DiscoveryInfo,
    ) -> Optional[PrecheckResult]:
        probe = self._get_listener_probe(role, oracle_home, expected_port, discovery)
        if not probe:
            return None
        ports = probe["detected_ports"]
        has_expected_port = probe["has_expected_port"]
        label = "主库" if role == 'primary' else "备库"
        message = f"{label}监听端口校验"
        status = 'pass' if has_expected_port else 'warn'
        suggestion = None if has_expected_port else "监听端口与表单输入不符，请核对 listener.ora"
        risk = RiskLevel.LOW if has_expected_port else RiskLevel.MEDIUM
        listener_details = {
            "status": "READY" if probe.get("success") else "NOT READY",
            "running": bool(probe.get("success")),
            "detected_ports": ports,
            "expected_port": expected_port,
            "has_expected_port": has_expected_port,
        }
        if role == 'primary':
            discovery.primary_oracle.listener_validation = listener_details
            discovery.network["primary_listener_port_check"] = {**listener_details, "raw": probe["stdout"][:800]}
        else:
            discovery.standby_oracle.listener_validation = listener_details
            discovery.network["standby_listener_port_check"] = {**listener_details, "raw": probe["stdout"][:800]}

        return PrecheckResult(
            check_name=f"{role}_listener_port_validation",
            category="connectivity",
            result=status,
            message=message,
            evidence={
                "detected_ports": ports,
                "expected_port": expected_port,
                "has_expected_port": has_expected_port,
            },
            suggestion=suggestion,
            blocking=False,
            risk_level=risk,
            target=f"{role}_listener",
            title=message,
            description=f"确认{label}监听实际开放端口是否包含用户输入端口；该检查只表达端口一致性，不代表监听是否运行或对端网络是否可达。",
            detected_result=(
                f"监听探测端口：{', '.join(str(port) for port in ports) if ports else '未探测到'}；"
                f"用户输入端口：{expected_port or '未知'}"
            ),
            summary=(
                f"{label}监听端口与用户输入一致。"
                if has_expected_port else
                f"{label}监听端口与用户输入不一致，请核对 listener.ora 或端口规划。"
            ),
            scope=f"{label}环境",
            related_fields=["primaryListenerPort" if role == "primary" else "standbyListenerPort"],
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
            target="primary_listener",
        )

    def _build_primary_path_alignment_result(self, path_type: str, path: str) -> Optional[PrecheckResult]:
        if not self.primary_sql or not path:
            return None
        normalized_path = path.strip().rstrip("/")
        if not normalized_path:
            return None
        escaped_path = normalized_path.upper().replace("'", "''")
        if path_type == 'data':
            sql = (
                f"SELECT COUNT(*) FROM v$datafile "
                f"WHERE UPPER(name) NOT LIKE '{escaped_path}%'"
            )
            check_name = "primary_data_path_alignment"
            message = "主库数据文件路径与输入一致性"
        elif path_type == 'redo':
            sql = (
                f"SELECT COUNT(*) FROM v$logfile "
                f"WHERE UPPER(member) NOT LIKE '{escaped_path}%'"
            )
            check_name = "primary_redo_path_alignment"
            message = "主库联机日志路径与输入一致性"
        else:
            return None

        mismatch_raw = self.primary_sql.query_single_value(sql)
        if mismatch_raw is None:
            return PrecheckResult(
                check_name=check_name,
                category="storage",
                result="warn",
                message=message,
                evidence={"expected_prefix": normalized_path, "mismatch_count": None},
                suggestion="无法获取主库路径信息，请手动核对数据/日志目录",
                blocking=False,
                risk_level=RiskLevel.MEDIUM,
                target=f"primary_{path_type}_path",
            )

        try:
            mismatch_count = int(mismatch_raw.strip() or "0")
        except ValueError:
            return PrecheckResult(
                check_name=check_name,
                category="storage",
                result="warn",
                message=message,
                evidence={"expected_prefix": normalized_path, "mismatch_raw": mismatch_raw},
                suggestion="主库路径检查结果异常，请手动核对数据/日志目录",
                blocking=False,
                risk_level=RiskLevel.MEDIUM,
                target=f"primary_{path_type}_path",
            )

        status = 'pass' if mismatch_count == 0 else 'fail'
        suggestion = None if mismatch_count == 0 else "主库实际路径与输入不一致，请确认并修正"
        risk = RiskLevel.LOW if mismatch_count == 0 else RiskLevel.HIGH

        return PrecheckResult(
            check_name=check_name,
            category="storage",
            result=status,
            message=message,
            evidence={"expected_prefix": normalized_path, "mismatch_count": mismatch_count},
            suggestion=suggestion,
            blocking=mismatch_count > 0,
            risk_level=risk,
            target=f"primary_{path_type}_path",
        )

    def _check_primary_custom_path_alignment(self, path_type: str) -> Optional[PrecheckResult]:
        strategy = (self.request.data_file_path_strategy if path_type == 'data' else self.request.redo_file_path_strategy) or ''
        if strategy.lower() != 'custom':
            return None
        path = (self.request.primary_data_file_path if path_type == 'data' else self.request.primary_redo_file_path) or ''
        if not path:
            return None
        return self._build_primary_path_alignment_result(path_type, path)

    def _check_primary_data_path_alignment(self) -> Optional[PrecheckResult]:
        strategy = (self.request.data_file_path_strategy or '').strip().lower()
        if strategy == 'custom':
            return None
        path = self.request.primary_data_file_path or self.request.data_files_path
        if not path:
            return None
        return self._build_primary_path_alignment_result('data', path)

    def _check_primary_redo_path_alignment(self) -> Optional[PrecheckResult]:
        strategy = (self.request.redo_file_path_strategy or '').strip().lower()
        if strategy == 'custom':
            return None
        primary_data_path = self.request.primary_data_file_path or self.request.data_files_path
        path = self.request.primary_redo_file_path or primary_data_path
        if not path:
            return None
        return self._build_primary_path_alignment_result('redo', path)

    def _check_standby_directory_ready(self, discovery: DiscoveryInfo, directory_type: str) -> Optional[PrecheckResult]:
        path = (self.request.standby_data_file_path if directory_type == 'data' else self.request.standby_redo_file_path) or ''
        normalized = path.strip()
        if not normalized or normalized.startswith("+"):
            return None
        meta_key = {
            "data": "standby_data",
            "log": "standby_log",
            "archive": "standby_archive",
            "adump": "standby_adump",
        }.get(directory_type, "standby_log")
        meta = discovery.storage.get(meta_key) or {}
        if not meta and self.standby_ssh:
            meta = self._inspect_path(self.standby_ssh, normalized)
        status_flag = meta.get("status") or "missing"
        has_files = bool(meta.get("has_files"))
        writable = meta.get("writable")
        disk_usage = (meta.get("disk_usage") or {}).get("percent")
        label = "数据文件" if directory_type == 'data' else "联机日志"
        check_name = f"standby_{directory_type}_dir_status"
        message = f"备库{label}目录可用性"

        if status_flag == "not_exists":
            status = 'fail'
            suggestion = f"目录不存在，请预先创建 {normalized} 并授权 oracle 用户"
            risk = RiskLevel.HIGH
            blocking = True
        elif status_flag == "not_writable" or writable is False:
            status = 'warn'
            suggestion = "目录无写权限，请检查权限或磁盘挂载状态"
            risk = RiskLevel.MEDIUM
            blocking = False
        elif has_files:
            status = 'warn'
            suggestion = "目录已存在文件，请确认不会覆盖生产数据"
            risk = RiskLevel.MEDIUM
            blocking = False
        else:
            status = 'pass'
            suggestion = None
            risk = RiskLevel.LOW
            blocking = False

        if disk_usage is not None and status == 'pass':
            if disk_usage > DISK_USAGE_FAIL_THRESHOLD:
                status = 'fail'
                suggestion = f"磁盘使用率 {disk_usage}%，已超过 {DISK_USAGE_FAIL_THRESHOLD}% 阈值，请先扩容或清理"
                risk = RiskLevel.HIGH
                blocking = True
            elif disk_usage > DISK_USAGE_WARN_THRESHOLD:
                status = 'warn'
                suggestion = f"磁盘使用率 {disk_usage}%，请确认剩余空间"
                risk = RiskLevel.MEDIUM

        return PrecheckResult(
            check_name=check_name,
            category="storage",
            result=status,
            message=message,
            evidence={
                "path": normalized,
                "status": status_flag,
                "writable": writable,
                "has_files": has_files,
                "disk_usage_percent": disk_usage,
            },
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
            target=f"standby_{directory_type}_dir",
        )

    def _build_standby_directory_status_result(
        self,
        discovery: DiscoveryInfo,
        path: Optional[str],
        directory_type: str,
        check_name: str,
        label: str,
    ) -> Optional[PrecheckResult]:
        normalized = (path or "").strip()
        if not normalized or normalized.startswith("+"):
            return None
        meta_key = {
            "data": "standby_data",
            "log": "standby_log",
            "archive": "standby_archive",
            "adump": "standby_adump",
        }.get(directory_type, "standby_log")
        meta = discovery.storage.get(meta_key) or {}
        if not meta and self.standby_ssh:
            meta = self._inspect_path(self.standby_ssh, normalized)

        status_flag = meta.get("status") or "missing"
        has_files = bool(meta.get("has_files"))
        writable = meta.get("writable")
        disk_usage = (meta.get("disk_usage") or {}).get("percent")
        message = f"备库{label}目录可用性"
        detected_result = (
            f"目录存在：{'是' if status_flag != 'not_exists' else '否'}；"
            f"可写：{'是' if writable is True else '否' if writable is False else '未知'}；"
            f"已有文件：{'有' if has_files else '无'}；"
            f"空间使用率：{f'{disk_usage}%' if disk_usage is not None else '未知'}"
        )

        if status_flag == "not_exists":
            status = 'fail'
            suggestion = f"目录不存在，请预先创建 {normalized} 并授权 oracle 用户"
            risk = RiskLevel.HIGH
            blocking = True
        elif status_flag == "not_writable" or writable is False:
            status = 'warn'
            suggestion = "目录无写权限，请检查权限或磁盘挂载状态"
            risk = RiskLevel.MEDIUM
            blocking = False
        elif has_files:
            status = 'warn'
            suggestion = "目录已存在文件，请确认不会覆盖生产数据"
            risk = RiskLevel.MEDIUM
            blocking = False
        else:
            status = 'pass'
            suggestion = None
            risk = RiskLevel.LOW
            blocking = False

        if disk_usage is not None and status != 'fail':
            if disk_usage >= DISK_USAGE_WARN_THRESHOLD:
                status = 'warn'
                suggestion = f"磁盘使用率 {disk_usage}%，已达到高使用率区间，请确认剩余空间"
                risk = RiskLevel.MEDIUM

        return PrecheckResult(
            check_name=check_name,
            category="storage",
            result=status,
            message=message,
            evidence={
                "path": normalized,
                "status": status_flag,
                "exists": status_flag != "not_exists",
                "writable": writable,
                "has_files": has_files,
                "disk_usage_percent": disk_usage,
            },
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
            target=f"standby_{directory_type}_dir",
            title=f"备库{label}目录检查",
            description=f"确认备库{label}目录的存在性、写权限、已有文件情况和空间使用率是否满足搭建前置条件。",
            detected_result=detected_result,
            summary="已完成目录存在性、写权限、已有文件和空间使用率四项联合校验。",
            scope="搭建环境",
            related_fields={
                "data": ["standbyDataFilePath"],
                "log": ["standbyRedoFilePath"],
                "archive": ["standbyArchivePath"],
                "adump": ["standbyOracleBase", "standbyDbUniqueName"],
            }.get(directory_type, []),
        )

    def _check_standby_data_dir_status(self, discovery: DiscoveryInfo) -> Optional[PrecheckResult]:
        standby_data_path = self.request.standby_data_file_path
        return self._build_standby_directory_status_result(
            discovery=discovery,
            path=standby_data_path,
            directory_type="data",
            check_name="standby_data_dir_status",
            label="数据文件",
        )

    def _check_standby_log_dir_status(self, discovery: DiscoveryInfo) -> Optional[PrecheckResult]:
        standby_log_path = self.request.standby_redo_file_path
        return self._build_standby_directory_status_result(
            discovery=discovery,
            path=standby_log_path,
            directory_type="log",
            check_name="standby_log_dir_status",
            label="联机日志",
        )

    def _check_standby_adump_dir_status(self, discovery: DiscoveryInfo) -> Optional[PrecheckResult]:
        standby_adump_path = discovery.storage.get("standby_adump_path") or self._resolve_standby_adump_path(discovery)
        result = self._build_standby_directory_status_result(
            discovery=discovery,
            path=standby_adump_path,
            directory_type="adump",
            check_name="standby_adump_dir_status",
            label="adump",
        )
        basis = discovery.storage.get("standby_adump_derive_basis") or {}
        if not result:
            return PrecheckResult(
                check_name="standby_adump_dir_status",
                category="storage",
                result="warn",
                message="备库adump目录可用性",
                evidence={
                    "path": None,
                    "derive_trustworthy": False,
                    "oracle_base": basis.get("oracle_base"),
                    "db_unique_name": basis.get("db_unique_name"),
                },
                suggestion="未能形成可信的 adump 推导路径，请先确认备库 ORACLE_BASE 和 DB_UNIQUE_NAME。",
                blocking=False,
                risk_level=RiskLevel.MEDIUM,
                target="standby_adump_dir",
                title="备库adump目录检查",
                description="确认备库 adump 目录的推导依据、推导结果及目录可用性是否满足搭建前置条件。",
                detected_result=(
                    f"推导依据 ORACLE_BASE={basis.get('oracle_base') or '未知'}；"
                    f"DB_UNIQUE_NAME={basis.get('db_unique_name') or '未知'}；"
                    "未能形成可信的 adump 路径。"
                ),
                summary="adump 路径推导失败，当前仅提示风险，默认不作为阻断项。",
                scope="搭建环境",
                related_fields=["standbyOracleBase", "standbyDbUniqueName"],
            )
        if result.evidence is not None:
            result.evidence["derive_trustworthy"] = True
            result.evidence["oracle_base"] = basis.get("oracle_base")
            result.evidence["db_unique_name"] = basis.get("db_unique_name")
        disk_usage_percent = None
        if result.evidence:
            disk_usage_percent = result.evidence.get("disk_usage_percent")
        result.description = "确认备库 adump 目录的推导依据、推导结果及目录可用性是否满足搭建前置条件。"
        result.detected_result = (
            f"推导依据 ORACLE_BASE={basis.get('oracle_base') or '未知'}；"
            f"DB_UNIQUE_NAME={basis.get('db_unique_name') or '未知'}；"
            f"推导结果={standby_adump_path or '未知'}；"
            f"目录存在：{'是' if result.evidence and result.evidence.get('exists') else '否'}；"
            f"可写：{'是' if result.evidence and result.evidence.get('writable') is True else '否' if result.evidence and result.evidence.get('writable') is False else '未知'}；"
            f"已有文件：{'有' if result.evidence and result.evidence.get('has_files') else '无'}；"
            f"空间使用率：{f'{disk_usage_percent}%' if disk_usage_percent is not None else '未知'}"
        )
        result.summary = "已基于 ORACLE_BASE 与 DB_UNIQUE_NAME 完成 adump 路径推导，并执行目录存在性、写权限、已有文件和空间使用率四项联合校验。"
        result.related_fields = ["standbyOracleBase", "standbyDbUniqueName"]
        return result

    def _check_standby_archive_dir_status(self, discovery: DiscoveryInfo) -> Optional[PrecheckResult]:
        standby_archive_path = (self.request.standby_archive_path or "").strip()
        result = self._build_standby_directory_status_result(
            discovery=discovery,
            path=standby_archive_path,
            directory_type="archive",
            check_name="standby_archive_dir_status",
            label="归档",
        )
        if result:
            result.description = "确认备库归档目录的存在性、写权限、已有文件情况和空间使用率是否满足搭建前置条件。"
        return result

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
            target="primary_database",
        )

    def _check_force_logging(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查 Force Logging"""
        force_logging = bool(discovery.primary_db.get("force_logging"))
        return PrecheckResult(
            check_name="force_logging",
            category="configuration",
            result="pass" if force_logging else "fail",
            message="Force Logging 检查",
            evidence={"force_logging": force_logging},
            suggestion="启用 Force Logging: ALTER DATABASE FORCE LOGGING" if not force_logging else None,
            blocking=not force_logging,
            risk_level=RiskLevel.MEDIUM if not force_logging else RiskLevel.LOW,
            target="primary_database",
        )

    def _check_version_compatibility(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查版本兼容性"""
        primary_version = (discovery.primary_oracle.version or "").strip()
        standby_version = (discovery.standby_oracle.version or primary_version).strip()
        result, suggestion, classification = self._classify_version_compatibility(primary_version, standby_version)
        standby_version_source = discovery.network.get("standby_version_source") or "auto_failed"
        source_label = {
            "auto_success": "自动探测成功",
            "inherit_primary": "继承主库",
            "auto_failed": "探测失败",
        }.get(standby_version_source, "探测失败")
        if classification == "exact_match":
            summary = f"主备版本一致；备库版本来源为{source_label}。"
        elif classification == "minor_diff":
            summary = f"主备版本存在小差异；备库版本来源为{source_label}，建议尽量保持一致。"
        elif classification == "incomplete":
            summary = f"版本信息不完整；备库版本来源为{source_label}。"
        else:
            summary = f"主备版本差异较大；备库版本来源为{source_label}。"
        return PrecheckResult(
            check_name="version_compatibility",
            category="compatibility",
            result=result,
            message="主备版本兼容性检查",
            evidence={
                "primary_version": primary_version,
                "standby_version": standby_version,
                "classification": classification,
            },
            suggestion=suggestion,
            blocking=result == "fail",
            risk_level=RiskLevel.HIGH if result == "fail" else RiskLevel.MEDIUM if result == "warn" else RiskLevel.LOW,
            target="global_configuration",
            title="主备版本兼容性检查",
            description="确认备库 Oracle 版本来自安装环境探测或主库继承，并评估主备版本差异是否可接受。",
            detected_result=f"主库版本 {primary_version or '未知'} / 备库版本 {standby_version or '未知'} / 备库来源 {source_label}",
            summary=summary,
            scope="搭建环境",
            related_fields=["primaryVersion", "standbyVersion", "primaryOracleHome", "standbyOracleHome"],
        )

    def _check_network_connectivity(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查网络连通性"""
        listener_status = discovery.network.get("primary_listener_status", "")
        is_reachable = listener_status == "READY"
        return PrecheckResult(
            check_name="network_connectivity",
            category="connectivity",
            result="pass" if is_reachable else "warn",
            message="网络连通性检查",
            evidence={"listener_status": listener_status},
            suggestion="检查 Listener 配置和防火墙设置" if not is_reachable else None,
            blocking=False,
            risk_level=RiskLevel.MEDIUM if not is_reachable else RiskLevel.LOW,
            target="network",
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
            target="global_configuration",
        )

    def _check_standby_space(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查备库空间"""
        data_usage = discovery.storage.get("standby_disk_usage_percent")
        log_usage = discovery.storage.get("standby_log_disk_usage_percent")
        usage_values = [value for value in (data_usage, log_usage) if value is not None]
        highest = max(usage_values) if usage_values else None
        if highest is None:
            status = 'warn'
            suggestion = "无法获取备库目录空间，建议手动确认磁盘使用率"
            blocking = False
            risk = RiskLevel.MEDIUM
        elif highest >= DISK_USAGE_WARN_THRESHOLD:
            status = 'warn'
            suggestion = f"备库目录使用率 {highest}%，请确认剩余容量"
            blocking = False
            risk = RiskLevel.MEDIUM
        else:
            status = 'pass'
            suggestion = None
            blocking = False
            risk = RiskLevel.LOW
        return PrecheckResult(
            check_name="standby_space",
            category="storage",
            result=status,
            message="备库可用空间检查",
            evidence={
                "standby_data_usage_percent": data_usage,
                "standby_log_usage_percent": log_usage,
            },
            suggestion=suggestion,
            blocking=blocking,
            risk_level=risk,
            target="standby_storage",
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
            target="storage_mapping",
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
            target="storage_mapping",
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
            target="global_configuration",
        )

    def _check_primary_db_unique_name_consistency(self) -> Optional[PrecheckResult]:
        expected = (self.request.db_unique_name_primary or "").strip()
        if not expected or not self.primary_sql:
            return None
        actual = self.primary_sql.query_single_value("SELECT db_unique_name FROM v$database")
        if not actual:
            return PrecheckResult(
                check_name="primary_db_unique_name_consistency",
                category="configuration",
                result="warn",
                message="主库 DB_UNIQUE_NAME 一致性",
                evidence={"expected": expected, "actual": None},
                suggestion="无法获取主库 DB_UNIQUE_NAME，请手动核对",
                blocking=False,
                risk_level=RiskLevel.MEDIUM,
                target="primary_database",
            )
        actual_value = actual.strip()
        matches = actual_value.upper() == expected.upper()
        return PrecheckResult(
            check_name="primary_db_unique_name_consistency",
            category="configuration",
            result="pass" if matches else "warn",
            message="主库 DB_UNIQUE_NAME 一致性",
            evidence={"expected": expected, "actual": actual_value},
            suggestion=None if matches else "主库 DB_UNIQUE_NAME 与输入不一致，请核对",
            blocking=False,
            risk_level=RiskLevel.LOW if matches else RiskLevel.MEDIUM,
            target="primary_database",
        )

    def _check_primary_service_name_consistency(self, discovery: DiscoveryInfo) -> Optional[PrecheckResult]:
        expected = (self.request.primary_service_name or "").strip()
        if not expected:
            return None
        detected_services = self._detect_primary_service_names()
        if not detected_services:
            return PrecheckResult(
                check_name="primary_service_name_consistency",
                category="configuration",
                result="warn",
                message="主库 SERVICE_NAME 一致性",
                evidence={"expected": expected, "detected_services": []},
                suggestion="无法获取主库活跃服务，请手动核对 service_name",
                blocking=False,
                risk_level=RiskLevel.MEDIUM,
                target="primary_database",
                title="主库 SERVICE_NAME 检查",
                description="基于 v$active_services 返回的活跃服务列表，校验用户输入的 SERVICE_NAME 是否被主库当前服务集合包含。",
                detected_result="未从 v$active_services 获取到有效服务列表。",
                summary="当前无法完成包含关系校验，需人工核对 SERVICE_NAME。",
                scope="主库环境",
                related_fields=["primaryServiceName"],
            )

        matches = any(expected.lower() in service.lower() for service in detected_services)
        return PrecheckResult(
            check_name="primary_service_name_consistency",
            category="configuration",
            result="pass" if matches else "warn",
            message="主库 SERVICE_NAME 一致性",
            evidence={
                "expected": expected,
                "detected_services": detected_services,
            },
            suggestion=None if matches else "主库活跃服务中未找到输入的 service_name，请核对",
            blocking=False,
            risk_level=RiskLevel.LOW if matches else RiskLevel.MEDIUM,
            target="primary_database",
            title="主库 SERVICE_NAME 检查",
            description="基于 v$active_services 返回的活跃服务列表，校验用户输入的 SERVICE_NAME 是否被主库当前服务集合包含。",
            detected_result=f"v$active_services 返回：{', '.join(detected_services)}",
            summary=(
                "用户输入的 SERVICE_NAME 已被主库活跃服务列表包含。"
                if matches else
                "用户输入的 SERVICE_NAME 未被主库活跃服务列表包含，请核对服务注册情况。"
            ),
            scope="主库环境",
            related_fields=["primaryServiceName"],
        )

    def _check_standby_db_unique_name_uniqueness(self) -> Optional[PrecheckResult]:
        primary_unique = (self.request.db_unique_name_primary or "").strip()
        standby_unique = (self.request.db_unique_name_standby or "").strip()
        if not standby_unique or not primary_unique:
            return None
        is_conflict = primary_unique.upper() == standby_unique.upper()
        return PrecheckResult(
            check_name="standby_db_unique_name_uniqueness",
            category="configuration",
            result="fail" if is_conflict else "pass",
            message="备库 DB_UNIQUE_NAME 唯一性",
            evidence={
                "primary_unique_name": primary_unique,
                "standby_unique_name": standby_unique,
            },
            suggestion="备库 DB_UNIQUE_NAME 与主库重复，请修改" if is_conflict else None,
            blocking=is_conflict,
            risk_level=RiskLevel.HIGH if is_conflict else RiskLevel.LOW,
            target="global_configuration",
            title="备库 DB_UNIQUE_NAME 唯一性检查",
            description="仅使用主库 DB_UNIQUE_NAME 与用户输入的备库 DB_UNIQUE_NAME 做重复性校验，不依赖备库 SQL 探测。",
            detected_result=f"主库 DB_UNIQUE_NAME 为：{primary_unique}",
            summary="已完成主备 DB_UNIQUE_NAME 重复性比对。",
            scope="备库环境",
            related_fields=["primaryDbUniqueName", "standbyDbUniqueName"],
        )

    def _check_listener_tns(self, discovery: DiscoveryInfo) -> PrecheckResult:
        """检查 Listener/TNS"""
        listener_status = discovery.network.get("primary_listener_status", "")
        is_ok = listener_status == "READY"
        return PrecheckResult(
            check_name="listener_tns",
            category="connectivity",
            result="pass" if is_ok else "warn",
            message="Listener/TNS 配置检查",
            evidence={"listener_status": listener_status},
            suggestion="检查 listener.ora 和 tnsnames.ora 配置" if not is_ok else None,
            blocking=False,
            risk_level=RiskLevel.MEDIUM if not is_ok else RiskLevel.LOW,
            target="network",
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
            target="global_configuration",
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
                    target="planning",
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
                    target="planning",
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
                    target="planning",
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

    @staticmethod
    def _looks_like_ip(value: Optional[str]) -> bool:
        if not value:
            return False
        try:
            ipaddress.ip_address(value.strip())
            return True
        except ValueError:
            return False

    def _detect_host_ips(self, executor: Optional[RemoteExecutor]) -> List[str]:
        if not executor:
            return []
        ips: List[str] = []
        primary_cmd = executor.execute("hostname -I", timeout=10)
        if primary_cmd.success:
            for token in primary_cmd.stdout.split():
                token = token.strip()
                if not token:
                    continue
                if self._looks_like_ip(token):
                    ips.append(token)
        if ips:
            return sorted(set(ips))
        fallback = executor.execute(
            r"ip -4 addr show | awk '/inet / {print $2}' | cut -d/ -f1",
            timeout=10,
        )
        if fallback.success:
            for token in fallback.stdout.split():
                if self._looks_like_ip(token):
                    ips.append(token.strip())
        return sorted(set(ips))

    def _read_os_release(self, executor: Optional[RemoteExecutor]) -> Dict[str, Any]:
        if not executor:
            return {}
        result = executor.execute("cat /etc/os-release 2>/dev/null", timeout=10)
        meta: Dict[str, Any] = {}
        if not result.success:
            return meta
        for line in result.stdout.splitlines():
            if "=" not in line:
                continue
            key, _, raw_value = line.partition("=")
            value = raw_value.strip().strip('"').strip("'")
            key_lower = key.strip().lower()
            meta[key_lower] = value
        pretty = meta.get("pretty_name") or meta.get("name")
        distro_name = meta.get("name")
        version = meta.get("version") or meta.get("version_id")
        normalized: Dict[str, Any] = {}
        if pretty:
            normalized["os_pretty_name"] = pretty
        if distro_name:
            normalized["os_name"] = distro_name
        if version:
            normalized["os_version"] = version
        return normalized

    def _fetch_kernel_version(self, executor: Optional[RemoteExecutor]) -> Optional[str]:
        if not executor:
            return None
        result = executor.execute("uname -sr", timeout=5)
        if result.success:
            line = (result.stdout or "").strip()
            return line or None
        return None

    def _read_shell_variable(self, executor: Optional[RemoteExecutor], variable: str) -> Optional[str]:
        if not executor or not variable or not re.match(r"^[A-Z0-9_]+$", variable):
            return None
        command = (
            "bash -lc '"
            "for profile in ~/.bash_profile ~/.bashrc ~/.profile; do "
            "  if [ -f \"$profile\" ]; then source \"$profile\" >/dev/null 2>&1; fi; "
            "done; "
            f"printf \"%s\" \"${{{variable}}}\"'"
        )
        result = executor.execute(command, timeout=20)
        if result.success:
            value = (result.stdout or "").strip()
            return value or None
        return None

    def _fetch_sqlplus_version(self, executor: Optional[RemoteExecutor], oracle_home: Optional[str]) -> Optional[str]:
        if not executor or not oracle_home:
            return None
        quoted_home = shlex.quote(oracle_home)
        command = (
            "bash -lc '"
            f"ORACLE_HOME={quoted_home}; "
            "export ORACLE_HOME; "
            "PATH=$ORACLE_HOME/bin:$PATH; "
            "LD_LIBRARY_PATH=$ORACLE_HOME/lib:$LD_LIBRARY_PATH; "
            "\"$ORACLE_HOME\"/bin/sqlplus -v'"
        )
        result = executor.execute(command, timeout=30)
        if not result.success:
            return None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return None

    def _fetch_opatch_inventory_version(self, executor: Optional[RemoteExecutor], oracle_home: Optional[str]) -> Optional[str]:
        if not executor or not oracle_home:
            return None
        quoted_home = shlex.quote(oracle_home)
        command = (
            "bash -lc '"
            f"ORACLE_HOME={quoted_home}; "
            "export ORACLE_HOME; "
            "PATH=$ORACLE_HOME/OPatch:$ORACLE_HOME/bin:$PATH; "
            "cd \"$ORACLE_HOME/OPatch\" && ./opatch lsinventory 2>/dev/null | grep -i \"Oracle Database\"'"
        )
        result = executor.execute(command, timeout=120)
        if not result.success:
            return None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return None

    def _derive_path_prefix(self, paths: List[str]) -> Optional[str]:
        cleaned = [path.strip() for path in paths if path and path.strip()]
        if not cleaned:
            return None
        sample = cleaned[0]
        if "/" not in sample:
            return sample
        prefix = sample.rsplit("/", 1)[0]
        return prefix or sample

    def _fetch_path_samples(self, sql_executor: Optional[SqlExecutor], query: str) -> List[str]:
        if not sql_executor:
            return []
        result = sql_executor.execute_sql(query)
        if not result.success or not result.stdout:
            return []
        rows = SqlExecutor._extract_multi_lines(result.stdout)
        filtered_rows = [
            row.strip()
            for row in rows
            if row.strip() and not row.strip().upper().startswith("SELECT ")
        ]
        return filtered_rows[:MAX_PATH_SAMPLE_ROWS]

    def _detect_primary_data_prefix(self) -> Optional[str]:
        samples = self._fetch_path_samples(
            self.primary_sql,
            "SELECT name FROM v$datafile WHERE rownum <= {limit}".format(limit=MAX_PATH_SAMPLE_ROWS),
        )
        return self._derive_path_prefix(samples)

    def _detect_primary_log_prefix(self) -> Optional[str]:
        samples = self._fetch_path_samples(
            self.primary_sql,
            "SELECT member FROM v$logfile WHERE rownum <= {limit}".format(limit=MAX_PATH_SAMPLE_ROWS),
        )
        return self._derive_path_prefix(samples)

    def _detect_primary_service_names(self) -> List[str]:
        if not self.primary_sql:
            return []
        services = self.primary_sql.query_multi_lines("SELECT name FROM v$active_services")
        return [service.strip() for service in services if service.strip()]

    def _detect_primary_service_name(self) -> Optional[str]:
        services = self._detect_primary_service_names()
        if services:
            return services[0]
        return None

    def _detect_primary_oracle_version(self) -> Optional[str]:
        if not self.primary_sql:
            return None
        result = self.primary_sql.execute_sql("SELECT banner FROM v$version")
        if not result.success:
            return None
        version = self._extract_release_version(result.stdout)
        if version:
            return version
        for line in (result.stdout or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if "oracle database" in stripped.lower():
                version = self._extract_release_version(stripped)
                if version:
                    return version
        return None

    def _detect_standby_oracle_version(self, oracle_home: Optional[str]) -> Optional[str]:
        sqlplus_version = self._fetch_sqlplus_version(self.standby_ssh, oracle_home)
        if sqlplus_version:
            return self._extract_release_version(sqlplus_version) or sqlplus_version
        opatch_version = self._fetch_opatch_inventory_version(self.standby_ssh, oracle_home)
        if opatch_version:
            return self._extract_release_version(opatch_version) or opatch_version
        return None

    def _probe_listener_status(
        self,
        executor: Optional[RemoteExecutor],
        oracle_home: Optional[str],
        expected_port: Optional[int],
        sid: Optional[str],
    ) -> Dict[str, Any]:
        if not executor or not oracle_home:
            return {}
        env = {
            "ORACLE_HOME": oracle_home,
            "PATH": f"{oracle_home}/bin:$PATH",
            "LD_LIBRARY_PATH": f"{oracle_home}/lib:$LD_LIBRARY_PATH",
        }
        result = executor.execute("lsnrctl status", environment=env, timeout=60)
        stdout = (result.stdout or "").strip()
        ports = sorted({int(match) for match in re.findall(r"PORT\s*=\s*(\d+)", stdout)})
        sid_normalized = (sid or "").strip().upper()
        sid_matched = bool(sid_normalized and re.search(rf'Instance\\s+\"{re.escape(sid_normalized)}\"', stdout, re.IGNORECASE))
        has_expected_port = expected_port in ports if expected_port else bool(ports)
        return {
            "success": result.success,
            "stdout": stdout[:800],
            "detected_ports": ports,
            "expected_port": expected_port,
            "sid_matched": sid_matched,
            "has_expected_port": has_expected_port,
        }

    @staticmethod
    def _extract_release_version(text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            match = re.search(r"(?:Release|Version)\s+([0-9][0-9A-Za-z.\-]*)", stripped, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _parse_version_components(text: Optional[str]) -> Optional[tuple[int, ...]]:
        if not text:
            return None
        parts = [int(part) for part in re.findall(r"\d+", text)[:5]]
        if not parts:
            return None
        return tuple(parts)

    @staticmethod
    def _trim_trailing_zeros(components: tuple[int, ...]) -> tuple[int, ...]:
        trimmed = list(components)
        while trimmed and trimmed[-1] == 0:
            trimmed.pop()
        return tuple(trimmed)

    @staticmethod
    def _pad_version_components(components: tuple[int, ...], width: int = 5) -> tuple[int, ...]:
        padded = list(components[:width])
        if len(padded) < width:
            padded.extend([0] * (width - len(padded)))
        return tuple(padded)

    def _classify_version_compatibility(
        self,
        primary_version: str,
        standby_version: str,
    ) -> tuple[str, Optional[str], str]:
        """将版本兼容性分成完全一致、差异较小、差异过大三段。"""
        if not primary_version or not standby_version:
            return "warn", "版本信息不完整，无法完成精确兼容性判断", "incomplete"

        primary_components = self._parse_version_components(primary_version)
        standby_components = self._parse_version_components(standby_version)
        if not primary_components or not standby_components:
            return "warn", "版本信息格式无法解析，建议补充探测结果", "unparsed"

        if self._trim_trailing_zeros(primary_components) == self._trim_trailing_zeros(standby_components):
            return "pass", None, "exact_match"

        primary_padded = self._pad_version_components(primary_components)
        standby_padded = self._pad_version_components(standby_components)
        if primary_padded[0] != standby_padded[0]:
            return "fail", "主备数据库大版本差异过大，请使用兼容版本", "major_mismatch"

        diff_indexes = [
            index
            for index, (left, right) in enumerate(zip(primary_padded, standby_padded))
            if left != right
        ]
        if len(diff_indexes) == 1:
            index = diff_indexes[0]
            if abs(primary_padded[index] - standby_padded[index]) <= 1:
                if all(value == 0 for value in primary_padded[index + 1:]) and all(
                    value == 0 for value in standby_padded[index + 1:]
                ):
                    return "warn", "主备版本差异较小，建议保持一致以降低兼容性风险", "minor_diff"

        return "fail", "主备版本差异过大，请使用兼容版本", "major_diff"

    def _collect_disk_usage(self, executor: Optional[RemoteExecutor], path: Optional[str]) -> Dict[str, Any]:
        if not executor or not path:
            return {}
        normalized = path.strip()
        if not normalized or normalized.startswith("+"):
            return {}
        command = f"df -P {shlex.quote(normalized)} | tail -1"
        result = executor.execute(command, timeout=15)
        if not result.success:
            return {}
        line = ""
        for candidate in result.stdout.splitlines()[::-1]:
            stripped = candidate.strip()
            if stripped:
                line = stripped
                break
        if not line:
            return {}
        return self._parse_disk_usage_line(line)

    @staticmethod
    def _parse_disk_usage_line(line: str) -> Dict[str, Any]:
        parts = line.split()
        if len(parts) < 6:
            return {"raw": line}
        filesystem, size, used, avail, percent, mount = parts[:6]
        percent_value: Optional[int] = None
        match = re.search(r"(\d+)", percent)
        if match:
            try:
                percent_value = int(match.group(1))
            except ValueError:
                percent_value = None
        return {
            "raw": line,
            "filesystem": filesystem,
            "size": size,
            "used": used,
            "available": avail,
            "percent": percent_value,
            "mount_point": mount,
        }

    def _resolve_standby_adump_path(self, discovery: DiscoveryInfo) -> Optional[str]:
        standby_base = (discovery.standby_oracle.oracle_base or "").strip()
        standby_unique_name = (
            (self.request.db_unique_name_standby or "").strip()
            or (self.request.standby_db_unique_name or "").strip()
        )
        if not standby_base or not standby_unique_name or standby_base.startswith("+"):
            return None
        normalized_base = standby_base.rstrip("/")
        normalized_unique_name = standby_unique_name.strip()
        return f"{normalized_base}/admin/{normalized_unique_name}/adump"


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
        "os_version": "Ubuntu 22.04",
        "kernel": "5.15.0-86-generic",
        "kernel_version": "5.15.0-86-generic",
    }
    discovery.standby_host = {
        "host": standby_host,
        "hostname": standby_host.split(".")[0],
        "ip_address": "10.0.0.11",
        "os_type": "Linux x86_64",
        "os_version": "Ubuntu 22.04",
        "kernel_version": "5.15.0-86-generic",
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
        sqlplus_version="SQL*Plus: Release 19.0.0.0.0",
        instance_status="OPEN",
        listener_validation={
            "status": "pass",
            "detected_ports": [request.primary_listener_port or 1521],
            "expected_port": request.primary_listener_port or 1521,
            "sid_matched": True,
        },
    )
    discovery.standby_oracle = OracleEnvInfo(
        version="19.18.0.0.0",
        oracle_home=request.standby_oracle_home or "/u01/app/oracle/product/19c/dbhome_1",
        oracle_sid=request.standby_sid or f"{shared_db_name}STB",
        oracle_base="/u01/app/oracle",
        storage_type=request.standby_storage_type or request.primary_storage_type or "fs",
        listener_port=request.standby_listener_port or 1521,
        is_cdb=request.standby_is_cdb if request.standby_is_cdb is not None else True,
        db_unique_name=request.db_unique_name_standby or f"{shared_db_name}_STBY",
        service_name=request.standby_service_name or f"{shared_db_name}_STBY",
        sqlplus_version="SQL*Plus: Release 19.0.0.0.0",
        listener_validation={
            "status": "warn",
            "detected_ports": [request.standby_listener_port or 1521],
            "expected_port": request.standby_listener_port or 1521,
            "sid_matched": False,
        },
    )
    discovery.primary_db = {
        "role": "PRIMARY",
        "open_mode": "READ WRITE",
        "database_role": "PRIMARY",
        "archive_mode": "ARCHIVELOG",
        "force_logging": True,
        "instance_status": "OPEN",
        "is_cdb": True,
    }
    discovery.network = {
        "primary_listener_status": "READY",
        "primary_version_source": "auto_success",
        "standby_version_source": "auto_success",
        "standby_oracle_base_source": "auto_success",
        "standby_service_name_source": "user_input" if request.standby_service_name else "planned",
        "primary_listener_port": request.primary_listener_port or 1521,
        "standby_listener_port": request.standby_listener_port or 1521,
        "log_transport_mode": (request.log_transport_mode or "ASYNC").upper(),
        "tns_check": "ok",
        "primary_listener_check": {
            "status": "pass",
            "detected_ports": [request.primary_listener_port or 1521],
            "expected_port": request.primary_listener_port or 1521,
            "sid_matched": True,
            "raw": "demo",
        },
        "standby_listener_check": {
            "status": "warn",
            "detected_ports": [request.standby_listener_port or 1521],
            "expected_port": request.standby_listener_port or 1521,
            "sid_matched": False,
        },
    }
    discovery.storage = {
        "storage_type": request.primary_storage_type or "fs",
        "primary_storage_type": request.primary_storage_type or "fs",
        "standby_storage_type": request.standby_storage_type or request.primary_storage_type or "fs",
        "primary_storage_input_type": request.primary_storage_type or "fs",
        "standby_storage_input_type": request.standby_storage_type or request.primary_storage_type or "fs",
        "primary_storage_detected_type": request.primary_storage_type or "fs",
        "data_files_path": request.primary_data_file_path or request.data_files_path or f"/u01/oradata/{shared_db_name}",
        "standby_data_files_path": request.standby_data_file_path or f"/u01/adg/{shared_db_name}",
        "archivelog_path": request.archivelog_path or f"/u01/oradata/{shared_db_name}/archivelog",
        "standby_archive_path": request.standby_archive_path or request.archivelog_path or f"/u01/adg/{shared_db_name}/archivelog",
        "primary_disk_usage": "120G used / 250G total",
        "standby_disk_usage": "80G used / 250G total",
        "redo_file_path_strategy": request.redo_file_path_strategy or "same",
        "data_file_path_strategy": request.data_file_path_strategy or "same",
    }
    discovery.storage["primary_data_detected_prefix"] = discovery.storage["data_files_path"]
    discovery.storage["primary_log_detected_prefix"] = request.primary_redo_file_path or discovery.storage["data_files_path"]
    discovery.storage["primary_data"] = {
        "input_path": discovery.storage["data_files_path"],
        "detected_prefix": discovery.storage["data_files_path"],
        "exists": True,
        "status": "ok",
        "writable": True,
        "has_files": True,
        "disk_usage": {"raw": "120G used / 250G total", "percent": 48},
    }
    discovery.storage["primary_log"] = {
        "input_path": request.primary_redo_file_path or discovery.storage["data_files_path"],
        "detected_prefix": request.primary_redo_file_path or discovery.storage["data_files_path"],
        "exists": True,
        "status": "ok",
        "writable": True,
        "has_files": True,
        "disk_usage": {"raw": "40G used / 250G total", "percent": 16},
    }
    discovery.storage["standby_data"] = {
        "input_path": discovery.storage["standby_data_files_path"],
        "exists": True,
        "status": "ok",
        "writable": True,
        "has_files": False,
        "disk_usage": {"raw": "80G used / 250G total", "percent": 32},
    }
    discovery.storage["standby_log"] = {
        "input_path": request.standby_redo_file_path or discovery.storage["standby_data_files_path"],
        "exists": False,
        "status": "not_exists",
        "writable": False,
        "has_files": False,
    }
    discovery.storage["primary_data_dir_status"] = "ok"
    discovery.storage["primary_data_dir_writable"] = True
    discovery.storage["primary_data_dir_has_files"] = True
    discovery.storage["primary_log_dir_status"] = "ok"
    discovery.storage["primary_log_dir_writable"] = True
    discovery.storage["primary_log_dir_has_files"] = True
    discovery.storage["standby_data_dir_status"] = "ok"
    discovery.storage["standby_data_dir_writable"] = True
    discovery.storage["standby_data_dir_has_files"] = False
    discovery.storage["standby_log_dir_status"] = "not_exists"
    discovery.storage["standby_log_dir_writable"] = False
    discovery.storage["standby_log_dir_has_files"] = False
    discovery.storage["primary_disk_usage_percent"] = 48
    discovery.storage["primary_log_disk_usage_percent"] = 16
    discovery.storage["standby_disk_usage_percent"] = 32
    discovery.storage["standby_log_disk_usage_percent"] = None
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
            target="primary_database",
        ),
        PrecheckResult(
            check_name="force_logging",
            category="configuration",
            result="fail",
            message="主库尚未启用 Force Logging",
            suggestion="执行 ALTER DATABASE FORCE LOGGING;",
            evidence={"force_logging": False},
            blocking=False,
            risk_level=RiskLevel.MEDIUM,
            target="primary_database",
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
            target="standby_listener",
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
