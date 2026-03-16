"""Oracle Data Guard 驱动实现"""

import json
import time
from copy import deepcopy
from pathlib import Path
from threading import Event, Lock, Timer
from typing import Dict, Any, Optional, List, Callable, Tuple
from datetime import datetime, timezone, timedelta

from loguru import logger

from .base import DatabaseHADriver
from ..core.executor import RemoteExecutor, SqlExecutor
from ..core.config import ClusterConfig
from .oracle_setup import OracleSetupExecutor, SetupProgress, OracleSetupStep, create_staged_executor


class OracleDataGuardDriver(DatabaseHADriver):
    """
    Oracle Data Guard 主备管理驱动

    实现完全基于 SSH + SQL*Plus/RMAN 的操作，不依赖 Data Guard Broker。
    """

    SWITCHOVER_TIMEOUTS = {
        "commit_to_standby": 600,
        "shutdown": 180,
        "startup": 300,
        "mount": 180,
        "managed_recovery": 180,
        "commit_to_primary": 600,
        "open_database": 300,
        "role_check": 60,
    }

    FAILOVER_TIMEOUTS = {
        "cancel_recovery": 120,
        "finish_recovery": 600,
        "commit_to_primary": 600,
        "open_database": 300,
        "role_check": 60,
    }

    DEFAULT_LISTENER_PORT = 1521

    def __init__(self, cluster_id: str, config: ClusterConfig):
        """
        初始化 Oracle Data Guard 驱动

        Args:
            cluster_id: 集群 ID
            config: 集群配置
        """
        super().__init__(cluster_id, config.dict())
        self.config = config

        # 初始化 SSH 执行器
        self.primary_ssh = RemoteExecutor(config.primary_ssh)
        self.standby_ssh = RemoteExecutor(config.standby_ssh)

        # 初始化 SQL 执行器
        self.primary_sql = SqlExecutor(
            self.primary_ssh,
            config.primary.oracle_home or "",
            config.primary.oracle_sid or ""
        )
        self.standby_sql = SqlExecutor(
            self.standby_ssh,
            config.standby.oracle_home or "",
            config.standby.oracle_sid or ""
        )

        base_dir = Path(__file__).resolve().parents[2]
        self._sync_history_dir = base_dir / "data" / "sync_history"
        self._sync_history_dir.mkdir(parents=True, exist_ok=True)
        self._status_lock = Lock()
        self._history_lock = Lock()
        self._history_interval = 60  # seconds
        self._history_timer: Optional[Timer] = None
        self._history_stop_event = Event()
        self._history_retention = timedelta(days=7)
        self._schedule_history_collection(initial_delay=1.0)

    def _create_setup_executor(
        self,
        config_payload: Dict[str, Any],
        log_callback: Optional[Callable[[str, str, str, str], None]] = None,
        status_callback: Optional[Callable[[str, str, int, str], None]] = None,
        cancel_callback: Optional[Callable[[], None]] = None
    ) -> Tuple[OracleSetupExecutor, SetupProgress]:
        """创建并初始化 OracleSetupExecutor"""
        progress = SetupProgress(
            total_steps=len(OracleSetupExecutor.STEP_SEQUENCE),
            log_callback=log_callback,
            status_callback=status_callback,
            cancel_callback=cancel_callback
        )
        progress.start()

        executor = OracleSetupExecutor(config_payload, progress)
        executor.primary_ssh = self.primary_ssh
        executor.primary_sql = self.primary_sql
        executor.standby_ssh = self.standby_ssh
        executor.standby_sql = self.standby_sql

        return executor, progress

    def _flatten_config_dict(self, data: Dict[str, Any], parent_key: str = "") -> Dict[str, Any]:
        """将嵌套配置展开为扁平字典，便于执行器使用"""
        flattened: Dict[str, Any] = {}

        for key, value in data.items():
            new_key = f"{parent_key}.{key}" if parent_key else key

            if isinstance(value, dict):
                flattened.update(self._flatten_config_dict(value, new_key))
            else:
                flattened[new_key] = value

        return flattened

    def _prepare_executor_config(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        基于集群默认配置与请求覆盖项构建执行器配置
        """
        base_payload = self._flatten_config_dict(self.config.model_dump())

        if overrides:
            if isinstance(overrides, dict):
                override_source = overrides
            elif hasattr(overrides, "dict") and callable(overrides.dict):
                override_source = overrides.dict()
            else:
                raise ValueError("setup 配置必须为字典或 Pydantic 模型")
            override_flat = self._flatten_config_dict(deepcopy(override_source))
            base_payload.update(override_flat)

        primary_cfg, standby_cfg, global_cfg, summary = self._parse_structured_configs(base_payload)
        base_payload["__primary_config__"] = primary_cfg
        base_payload["__standby_config__"] = standby_cfg
        base_payload["__global_config__"] = global_cfg
        base_payload["__config_summary__"] = summary
        return base_payload

    def _parse_structured_configs(
        self,
        flat_config: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        """解析主备与全局配置"""
        primary_cfg = self._build_role_config(flat_config, "primary")
        standby_cfg = self._build_role_config(flat_config, "standby", include_global_fallback=["oracle_sid"])
        global_cfg = self._build_global_config(flat_config, primary_cfg, standby_cfg)

        summary = {
            "primary": {
                "sid": primary_cfg.get("sid"),
                "db_unique_name": primary_cfg.get("db_unique_name"),
                "service_name": primary_cfg.get("service_name"),
                "listener_port": primary_cfg.get("listener_port"),
                "storage_type": primary_cfg.get("storage_type"),
                "is_cdb": primary_cfg.get("is_cdb"),
            },
            "standby": {
                "sid": standby_cfg.get("sid"),
                "db_unique_name": standby_cfg.get("db_unique_name"),
                "service_name": standby_cfg.get("service_name"),
                "listener_port": standby_cfg.get("listener_port"),
                "storage_type": standby_cfg.get("storage_type"),
                "is_cdb": standby_cfg.get("is_cdb"),
            },
            "global": {
                "log_transport_mode": global_cfg.get("log_transport_mode"),
                "auto_create_srl": global_cfg.get("auto_create_srl"),
                "data_file_path_strategy": global_cfg.get("data_file_path_strategy"),
                "redo_file_path_strategy": global_cfg.get("redo_file_path_strategy"),
                "archive_cleanup_policy": global_cfg.get("archive_cleanup_policy"),
                "standby_archive_path": global_cfg.get("standby_archive_path"),
            },
        }
        return primary_cfg, standby_cfg, global_cfg, summary

    def _build_role_config(
        self,
        config: Dict[str, Any],
        role: str,
        *,
        include_global_fallback: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """从扁平配置中提取指定角色的参数"""
        role_config: Dict[str, Any] = {"role": role}
        prefix = f"{role}."
        for key, value in config.items():
            if key.startswith(prefix):
                suffix = key[len(prefix):]
                if suffix and suffix not in role_config:
                    role_config[suffix] = value

        fallback_keys = include_global_fallback[:] if include_global_fallback else []
        sid_keys = [
            f"{role}.sid",
            f"{role}_sid",
            f"{role}.oracle_sid",
            f"{role}_oracle_sid",
        ] + fallback_keys
        if role == "standby":
            sid_keys.append("oracle_sid")
        sid_value = self._resolve_config_value(config, *sid_keys)
        if not self._is_empty_value(sid_value):
            role_config["sid"] = sid_value
            role_config.setdefault("oracle_sid", sid_value)

        oracle_home = self._resolve_config_value(
            config,
            f"{role}.oracle_home",
            f"{role}_oracle_home",
            "oracle_home" if role == "primary" else None
        )
        if not self._is_empty_value(oracle_home):
            role_config["oracle_home"] = oracle_home

        oracle_base = self._resolve_config_value(
            config,
            f"{role}.oracle_base",
            f"{role}_oracle_base"
        )
        if not self._is_empty_value(oracle_base):
            role_config["oracle_base"] = oracle_base

        db_unique_name = self._resolve_config_value(
            config,
            f"{role}.db_unique_name",
            f"{role}_db_unique_name",
            "db_unique_name"
        )
        if not self._is_empty_value(db_unique_name):
            role_config["db_unique_name"] = db_unique_name

        db_name = self._resolve_config_value(
            config,
            f"{role}.db_name",
            f"{role}_db_name",
            "db_name"
        )
        if not self._is_empty_value(db_name):
            role_config["db_name"] = db_name

        listener_port = self._coerce_int_value(
            self._resolve_config_value(
                config,
                f"{role}.listener_port",
                f"{role}_listener_port",
                "listener_port" if role == "primary" else None
            ),
            default=self.DEFAULT_LISTENER_PORT
        )
        role_config["listener_port"] = listener_port or self.DEFAULT_LISTENER_PORT

        service_name = self._resolve_config_value(
            config,
            f"{role}.service_name",
            f"{role}_service_name",
        )
        if self._is_empty_value(service_name):
            if role == "standby":
                service_name = role_config.get("sid") or role_config.get("db_name")
            else:
                service_name = role_config.get("db_name") or role_config.get("sid")
        role_config["service_name"] = service_name

        storage_type = self._resolve_config_value(
            config,
            f"{role}.storage_type",
            f"{role}_storage_type",
            "storage_type"
        )
        if not self._is_empty_value(storage_type):
            role_config["storage_type"] = storage_type

        is_cdb = self._coerce_bool_value(
            self._resolve_config_value(
                config,
                f"{role}.is_cdb",
                f"{role}_is_cdb",
                "is_cdb"
            )
        )
        if is_cdb is not None:
            role_config["is_cdb"] = is_cdb

        return role_config

    def _build_global_config(
        self,
        config: Dict[str, Any],
        primary_cfg: Dict[str, Any],
        standby_cfg: Dict[str, Any]
    ) -> Dict[str, Any]:
        """解析全局配置字段"""
        mode_value = self._resolve_config_value(config, "log_transport_mode", "dg.log_transport_mode")
        log_transport_mode = (str(mode_value).strip().upper() if mode_value else "ASYNC") or "ASYNC"
        auto_create_srl = self._coerce_bool_value(
            self._resolve_config_value(
                config,
                "auto_create_srl",
                "auto_create_standby_redo",
                "auto_create_standby_redo_logs"
            ),
            default=True
        )
        data_strategy = str(
            self._resolve_config_value(config, "data_file_path_strategy") or "same"
        ).strip().lower()
        redo_strategy = str(
            self._resolve_config_value(config, "redo_file_path_strategy") or "same"
        ).strip().lower()
        archive_cleanup_policy = str(
            self._resolve_config_value(config, "archive_cleanup_policy") or "none"
        ).strip().lower()
        archive_cleanup_param = self._resolve_config_value(config, "archive_cleanup_param")
        standby_archive_path = self._resolve_config_value(
            config,
            "standby_archive_path",
            "standby.archive_path",
            "standby.archivelog_path",
        )
        if self._is_empty_value(standby_archive_path):
            standby_archive_path = self._resolve_config_value(config, "archivelog_path") or getattr(
                self.config, "archivelog_path", None
            )

        global_config = {
            "log_transport_mode": log_transport_mode,
            "auto_create_srl": True if auto_create_srl is None else auto_create_srl,
            "data_file_path_strategy": data_strategy or "same",
            "redo_file_path_strategy": redo_strategy or "same",
            "standby_archive_path": standby_archive_path,
            "archive_cleanup_policy": archive_cleanup_policy or "none",
            "archive_cleanup_param": archive_cleanup_param,
            "primary_db_unique_name": primary_cfg.get("db_unique_name"),
            "standby_db_unique_name": standby_cfg.get("db_unique_name"),
        }
        return global_config

    @staticmethod
    def _resolve_config_value(
        config: Dict[str, Any],
        *keys: Optional[str],
        default: Any = None
    ) -> Any:
        for key in keys:
            if not key:
                continue
            if key not in config:
                continue
            value = config.get(key)
            if OracleDataGuardDriver._is_empty_value(value):
                continue
            return value
        return default

    @staticmethod
    def _is_empty_value(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str) and not value.strip():
            return True
        return False

    @staticmethod
    def _coerce_bool_value(value: Any, default: Optional[bool] = None) -> Optional[bool]:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1", "on"}:
                return True
            if normalized in {"false", "no", "0", "off"}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    @staticmethod
    def _coerce_int_value(value: Any, default: Optional[int] = None) -> Optional[int]:
        if value is None:
            return default
        try:
            if isinstance(value, str):
                cleaned = value.strip()
                if not cleaned:
                    return default
                return int(cleaned, 10)
            return int(value)
        except (TypeError, ValueError):
            return default

    def _serialize_setup_progress(self, progress_tracker: Any) -> Dict[str, Any]:
        """将阶段化进度转换为字典"""
        to_dict_fn = getattr(progress_tracker, "to_dict", None)
        if callable(to_dict_fn):
            return to_dict_fn()

        snapshot_getter = getattr(progress_tracker, "get_progress_snapshot", None)
        if callable(snapshot_getter):
            snapshot = snapshot_getter()
            model_dump_fn = getattr(snapshot, "model_dump", None)
            if callable(model_dump_fn):
                return model_dump_fn()
            dict_fn = getattr(snapshot, "dict", None)
            if callable(dict_fn):
                return dict_fn()
            if isinstance(snapshot, dict):
                return snapshot
            if hasattr(snapshot, "__dict__"):
                return snapshot.__dict__
        return {}

    def _extract_progress_logs(self, progress_tracker: Any) -> List[Dict[str, Any]]:
        """获取阶段化执行日志"""
        get_logs_fn = getattr(progress_tracker, "get_logs", None)
        if callable(get_logs_fn):
            try:
                logs = get_logs_fn()
                if logs is not None:
                    return list(logs)
            except Exception as exc:
                logger.warning(f"获取阶段日志失败: {exc}")

        logs_attr = getattr(progress_tracker, "progress_log", None)
        if logs_attr is None:
            return []
        return list(logs_attr)

    def setup(self, config: Dict[str, Any]) -> Callable[..., Dict[str, Any]]:
        """
        搭建 Oracle Data Guard 主备架构

        Args:
            config: 搭建配置参数

        Returns:
            后台任务执行函数
        """
        config_payload = self._prepare_executor_config(config or {})

        def _runner(logger, **_):
            return self._execute_setup_wrapper(logger, config_payload)

        return _runner

    def _execute_setup_wrapper(
        self,
        task_logger: Any,
        config_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """供任务管理器调用的 setup 执行器"""

        primary_host = self.config.primary_ssh.host
        standby_host = self.config.standby_ssh.host

        task_logger.info(
            f"开始搭建 Data Guard 主备，集群: {self.cluster_id}, 主库: {primary_host}, 备库: {standby_host}"
        )
        if hasattr(task_logger, "set_progress"):
            task_logger.set_progress(0, "初始化搭建流程")

        def _log_callback(step: str, level: str, message: str, _result: str):
            log_text = f"[{step}] {message}"
            log_method = getattr(task_logger, level, task_logger.info)
            log_method(log_text)

        def _status_callback(step: str, status: str, percent: int, message: str):
            progress_message = message or f"{step} -> {status}"
            if hasattr(task_logger, "set_progress"):
                task_logger.set_progress(percent, progress_message)

        cancel_callback = getattr(task_logger, "raise_if_cancelled", None)
        if cancel_callback:
            cancel_callback()

        executor, progress = create_staged_executor(
            config_payload,
            log_callback=_log_callback,
            status_callback=_status_callback,
            cancel_callback=cancel_callback,
            executors={
                "primary_ssh": self.primary_ssh,
                "standby_ssh": self.standby_ssh,
                "primary_sql": self.primary_sql,
                "standby_sql": self.standby_sql,
            }
        )

        try:
            stage_results = executor.execute_all_stages()
        finally:
            self.primary_ssh.close()
            self.standby_ssh.close()

        if hasattr(task_logger, "set_progress"):
            task_logger.set_progress(100, "搭建流程完成")

        task_logger.info("搭建流程执行完毕")

        setup_progress = self._serialize_setup_progress(progress)
        logs = self._extract_progress_logs(progress)

        logger.info(f"集群 {self.cluster_id} 搭建任务完成，结果: {stage_results}")

        return {
            "results": stage_results,
            "setup_progress": setup_progress,
            "logs": logs,
        }

    def generate_setup_commands(self) -> Dict[str, List[Dict[str, str]]]:
        """
        生成搭建流程的所有命令供前端审批

        Returns:
            Dict[str, List[Dict]]: 每个步骤的命令列表
        """
        # 临时连接用于生成命令
        executor = OracleSetupExecutor(
            self._prepare_executor_config(),
            SetupProgress()
        )
        executor.primary_ssh = self.primary_ssh
        executor.primary_sql = self.primary_sql
        executor.standby_ssh = self.standby_ssh
        executor.standby_sql = self.standby_sql

        return executor.generate_setup_commands()

    def execute_all_steps(
        self,
        skip_steps: Optional[List[str]] = None,
        log_callback: Optional[Callable[[str, str, str, str], None]] = None,
        status_callback: Optional[Callable[[str, str, int, str], None]] = None,
        cancel_callback: Optional[Callable[[], None]] = None
    ) -> Dict[str, Any]:
        """基于集群默认配置执行搭建步骤，可选择跳过部分步骤"""
        config_payload = self._prepare_executor_config()
        executor, _ = self._create_setup_executor(
            config_payload,
            log_callback=log_callback,
            status_callback=status_callback,
            cancel_callback=cancel_callback
        )

        skip_steps = skip_steps or []
        logger.info(
            "执行审批搭建流程，跳过步骤: {}".format(
                ", ".join(skip_steps) if skip_steps else "无"
            )
        )

        try:
            return executor.execute_all_steps(skip_steps=skip_steps)
        finally:
            self.primary_ssh.close()
            self.standby_ssh.close()

    def status(self) -> Dict[str, Any]:
        """
        获取 Data Guard 主备状态

        Returns:
            状态信息字典，包含：
            - primary: 主库信息（角色、保护模式、归档日志状态等）
            - standby: 备库信息（角色、同步状态、延迟等）
            - sync: 同步信息（gap、延迟秒数等）
        """
        logger.info(f"查询集群 {self.cluster_id} 的 Data Guard 状态")

        with self._status_lock:
            status = {
                "cluster_id": self.cluster_id,
                "cluster_name": self.config.cluster_name,
                "timestamp": datetime.utcnow().isoformat(),
                "primary": {},
                "standby": {},
                "sync": {}
            }

            try:
                # 连接主库
                if self.primary_ssh.connect():
                    status["primary"] = self._get_primary_status()
                    self.primary_ssh.close()

                # 连接备库
                if self.standby_ssh.connect():
                    status["standby"] = self._get_standby_status()
                    self.standby_ssh.close()

                # 计算同步状态
                status["sync"] = self._calculate_sync_status(status["primary"], status["standby"])

            except Exception as e:
                logger.error(f"获取状态失败: {e}")
                status["error"] = str(e)

            return status

    def get_sync_history(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """获取同步延迟历史数据"""
        return self._get_sync_history(self.cluster_id, start_time, end_time)

    def resource_status(self) -> Dict[str, Any]:
        """获取主库资源使用情况（表空间与归档日志）"""
        logger.info(f"查询集群 {self.cluster_id} 的资源使用情况")

        if not self.primary_ssh.connect():
            raise RuntimeError("无法连接主库 SSH，无法获取资源信息")

        try:
            return self._get_resource_status()
        finally:
            self.primary_ssh.close()

    def _get_resource_status(self) -> Dict[str, Any]:
        """查询表空间与归档日志使用情况"""

        def _parse_rows(raw_output: str, expected_cols: int) -> List[List[str]]:
            rows: List[List[str]] = []
            for raw_line in raw_output.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                upper_line = line.upper()
                if upper_line.startswith("SQL>") or upper_line.startswith("CONNECTED") or upper_line.startswith("DISCONNECTED"):
                    continue
                parts = [part.strip() for part in line.split("|")]
                if len(parts) > expected_cols:
                    head = parts[:expected_cols - 1]
                    tail = ["|".join(parts[expected_cols - 1:]).strip()]
                    parts = head + tail
                if len(parts) < expected_cols:
                    continue
                rows.append(parts[:expected_cols])
            return rows

        def _to_float(value: Optional[str]) -> float:
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        tablespace_sql = (
            "SELECT tablespace_name || '|' || "
            "ROUND(bytes / 1024 / 1024, 2) || '|' || "
            "ROUND(NVL(max_bytes, bytes) / 1024 / 1024, 2) "
            "FROM v$tablespace"
        )
        tablespace_result = self.primary_sql.execute_sql(tablespace_sql)
        if not tablespace_result.success:
            error = tablespace_result.stderr.strip() or "未知错误"
            raise RuntimeError(f"查询表空间使用情况失败: {error}")
        tablespace_rows = _parse_rows(tablespace_result.stdout, 3)

        tablespaces: List[Dict[str, Any]] = []
        for row in tablespace_rows:
            name, used_str, max_str = row
            used_mb = round(_to_float(used_str), 2)
            max_mb = round(_to_float(max_str), 2)
            usage_percent = round((used_mb / max_mb * 100) if max_mb else 0.0, 2)
            tablespaces.append({
                "tablespace_name": name,
                "mb_used": used_mb,
                "mb_max": max_mb,
                "usage_percent": usage_percent,
            })

        archive_sql = (
            "SELECT NVL(name, 'UNKNOWN') || '|' || "
            "ROUND(blocks * blocksize / 1024 / 1024, 2) "
            "FROM v$archived_log "
            "WHERE first_change# > (SELECT MAX(checkpoint_change#) FROM v$datafile)"
        )
        archive_result = self.primary_sql.execute_sql(archive_sql)
        if not archive_result.success:
            error = archive_result.stderr.strip() or "未知错误"
            raise RuntimeError(f"查询归档日志保留情况失败: {error}")
        archive_rows = _parse_rows(archive_result.stdout, 2)

        archive_logs: List[Dict[str, Any]] = []
        for row in archive_rows:
            log_name, size_str = row
            size_mb = round(_to_float(size_str), 2)
            archive_logs.append({
                "name": log_name,
                "size_mb": size_mb,
            })

        totals = {
            "tablespace_used_mb": round(sum(item["mb_used"] for item in tablespaces), 2),
            "tablespace_total_mb": round(sum(item["mb_max"] for item in tablespaces), 2),
            "archive_total_mb": round(sum(item["size_mb"] for item in archive_logs), 2),
        }

        return {
            "cluster_id": self.cluster_id,
            "collected_at": datetime.utcnow(),
            "tablespaces": tablespaces,
            "archive_logs": archive_logs,
            "totals": totals,
        }

    def _get_primary_status(self) -> Dict[str, Any]:
        """获取主库状态"""
        status = {
            "role": "PRIMARY",
            "host": self.config.primary_ssh.host,
            "connected": True
        }

        # 查询数据库角色
        role_sql = "SELECT database_role, open_mode, protection_mode FROM v$database"
        result = self.primary_sql.execute_sql(role_sql)

        if result.success:
            lines = result.stdout.strip().split("\n")
            if len(lines) >= 3:
                status["database_role"] = lines[0].strip()
                status["open_mode"] = lines[1].strip()
                status["protection_mode"] = lines[2].strip()

        # 查询归档日志序列
        archive_sql = "SELECT MAX(sequence#) FROM v$archived_log WHERE applied='YES'"
        result = self.primary_sql.execute_sql(archive_sql)

        if result.success:
            status["last_archived_sequence"] = result.stdout.strip()

        return status

    def _get_standby_status(self) -> Dict[str, Any]:
        """获取备库状态"""
        status = {
            "role": "STANDBY",
            "host": self.config.standby_ssh.host,
            "connected": True
        }

        # 查询数据库角色
        role_sql = "SELECT database_role, open_mode, protection_mode FROM v$database"
        result = self.standby_sql.execute_sql(role_sql)

        if result.success:
            lines = result.stdout.strip().split("\n")
            if len(lines) >= 3:
                status["database_role"] = lines[0].strip()
                status["open_mode"] = lines[1].strip()
                status["protection_mode"] = lines[2].strip()

        # 查询应用状态
        apply_sql = """
            SELECT process, status, thread#, sequence#
            FROM v$managed_standby
            WHERE process LIKE 'MRP%'
        """
        result = self.standby_sql.execute_sql(apply_sql)

        if result.success:
            lines = result.stdout.strip().split("\n")
            if lines:
                status["mrp_status"] = lines[0].strip()

        # 查询应用延迟
        lag_sql = "SELECT applied_seq#, last_applied FROM v$archive_dest_status WHERE dest_id=2"
        result = self.standby_sql.execute_sql(lag_sql)

        if result.success:
            lines = result.stdout.strip().split("\n")
            if len(lines) >= 2:
                status["applied_sequence"] = lines[0].strip()
                status["last_applied_time"] = lines[1].strip()

        return status

    def _calculate_sync_status(self, primary: Dict, standby: Dict) -> Dict[str, Any]:
        """计算同步状态"""
        sync_status = {
            "status": "UNKNOWN",
            "lag_seconds": 0,
            "gap": False
        }

        try:
            # 比较归档序列号
            primary_seq = primary.get("last_archived_sequence", "0")
            standby_seq = standby.get("applied_sequence", "0")

            if primary_seq and standby_seq:
                primary_seq = int(primary_seq)
                standby_seq = int(standby_seq)

                if primary_seq == standby_seq:
                    sync_status["status"] = "SYNCED"
                    sync_status["lag_sequence"] = 0
                else:
                    gap = primary_seq - standby_seq
                    sync_status["status"] = "LAGGING"
                    sync_status["lag_sequence"] = gap
                    sync_status["gap"] = gap > 0

        except (ValueError, TypeError) as e:
            logger.error(f"计算同步状态失败: {e}")

        lag_seconds = self._calculate_apply_lag_seconds(standby)
        if lag_seconds is not None:
            sync_status["lag_seconds"] = lag_seconds

        return sync_status

    def _calculate_apply_lag_seconds(self, standby: Dict[str, Any]) -> Optional[float]:
        """根据备库的 last_applied 时间估算延迟秒数"""
        last_applied = standby.get("last_applied_time")
        if not last_applied:
            return None

        applied_at = self._parse_datetime(str(last_applied))
        if not applied_at:
            return None

        lag = (datetime.now(timezone.utc) - applied_at).total_seconds()
        return round(max(lag, 0.0), 1)

    def _parse_datetime(self, value: str) -> Optional[datetime]:
        """解析多种常见格式的时间字符串"""
        if not value:
            return None

        text = value.strip()
        parsed: Optional[datetime] = None

        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            formats = [
                "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S",
                "%d-%b-%y %H:%M:%S",
                "%d-%b-%Y %H:%M:%S",
            ]
            for fmt in formats:
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue

        if not parsed:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)

        return parsed

    def _schedule_history_collection(self, initial_delay: Optional[float] = None):
        """启动或继续延迟数据定时采集"""
        if not hasattr(self, "_history_stop_event"):
            return

        if self._history_stop_event.is_set():
            return

        delay = self._history_interval if initial_delay is None else max(initial_delay, 0.0)

        def _runner():
            if self._history_stop_event.is_set():
                return
            try:
                self._collect_sync_history(self.cluster_id)
            except Exception as exc:
                logger.error(f"收集集群 {self.cluster_id} 延迟数据失败: {exc}")
            finally:
                if not self._history_stop_event.is_set():
                    self._schedule_history_collection()

        timer = Timer(delay, _runner)
        timer.daemon = True
        self._history_timer = timer
        timer.start()

    def _collect_sync_history(self, cluster_id: str):
        """采集当前同步延迟数据并持久化"""
        snapshot_time = datetime.now(timezone.utc)
        try:
            status_snapshot = self.status()
        except Exception as exc:
            logger.error(f"定时采集状态失败: {exc}")
            status_snapshot = {"sync": {}, "error": str(exc)}

        sync_payload = status_snapshot.get("sync") or {}
        lag_seconds = float(sync_payload.get("lag_seconds") or 0.0)
        status_text = sync_payload.get("status") or ("ERROR" if status_snapshot.get("error") else "UNKNOWN")

        entry = {
            "timestamp": snapshot_time.isoformat(),
            "lag_seconds": round(lag_seconds, 2),
            "status": status_text
        }

        history_file = self._history_file_path(cluster_id)

        with self._history_lock:
            history: List[Dict[str, Any]] = []
            if history_file.exists():
                try:
                    history = json.loads(history_file.read_text(encoding="utf-8"))
                    if not isinstance(history, list):
                        history = []
                except Exception as exc:
                    logger.warning(f"读取同步历史文件失败 ({history_file}): {exc}")
                    history = []

            history.append(entry)

            if self._history_retention:
                cutoff = snapshot_time - self._history_retention
                filtered: List[Dict[str, Any]] = []
                for item in history:
                    ts_obj = self._parse_history_timestamp(item.get("timestamp"))
                    if ts_obj and ts_obj >= cutoff:
                        filtered.append(item)
                history = filtered

            history.sort(key=lambda item: item.get("timestamp", ""))
            history_file.parent.mkdir(parents=True, exist_ok=True)
            history_file.write_text(
                json.dumps(history, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

    def _get_sync_history(
        self,
        cluster_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """读取历史同步延迟数据"""
        history_file = self._history_file_path(cluster_id)

        with self._history_lock:
            if not history_file.exists():
                return []
            try:
                raw_entries = json.loads(history_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error(f"解析同步历史 JSON 失败 ({history_file}): {exc}")
                return []

        if not isinstance(raw_entries, list):
            return []

        start_dt = self._normalize_datetime(start_time) if start_time else None
        end_dt = self._normalize_datetime(end_time) if end_time else None

        history: List[Dict[str, Any]] = []
        for entry in raw_entries:
            ts_obj = self._parse_history_timestamp(entry.get("timestamp"))
            if not ts_obj:
                continue
            if start_dt and ts_obj < start_dt:
                continue
            if end_dt and ts_obj > end_dt:
                continue

            history.append({
                "timestamp": ts_obj,
                "lag_seconds": float(entry.get("lag_seconds") or 0.0),
                "status": entry.get("status") or "UNKNOWN"
            })

        history.sort(key=lambda item: item["timestamp"])
        return history

    def _history_file_path(self, cluster_id: str) -> Path:
        """获取历史文件路径"""
        return self._sync_history_dir / f"{cluster_id}.json"

    def _normalize_datetime(self, value: datetime) -> datetime:
        """统一时间为 UTC"""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _parse_history_timestamp(self, value: Optional[str]) -> Optional[datetime]:
        """解析历史记录中的时间字符串"""
        if not value:
            return None
        return self._parse_datetime(value)

    def switchover(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        执行 Switchover（正常切换）

        Args:
            dry_run: 是否为演练模式

        Returns:
            切换结果
        """
        logger.info(f"执行 Switchover，集群: {self.cluster_id}, 演练模式: {dry_run}")

        result = {
            "action": "switchover",
            "cluster_id": self.cluster_id,
            "dry_run": dry_run,
            "status": "pending",
            "checks": {},
            "steps": []
        }

        try:
            # 预检查
            checks = self._switchover_precheck()
            result["checks"] = checks

            if not all(checks.values()):
                result["status"] = "failed"
                result["error"] = "预检查未通过，无法执行切换"
                return result

            if dry_run:
                result["status"] = "dry_run_success"
                result["message"] = "演练模式：预检查通过，未实际执行切换"
                return result

            if not self.primary_ssh.connect():
                raise RuntimeError("无法连接主库 SSH，终止切换")

            if not self.standby_ssh.connect():
                raise RuntimeError("无法连接备库 SSH，终止切换")

            primary_committed = False
            standby_promoted = False

            def mark_primary_committed():
                nonlocal primary_committed
                primary_committed = True

            def mark_standby_promoted():
                nonlocal standby_promoted
                standby_promoted = True

            try:
                self._demote_database_to_standby(
                    sql_executor=self.primary_sql,
                    steps_log=result["steps"],
                    node_label=f"primary@{self.config.primary_ssh.host}",
                    mark_committed=mark_primary_committed
                )

                self._promote_database_to_primary(
                    sql_executor=self.standby_sql,
                    steps_log=result["steps"],
                    node_label=f"standby@{self.config.standby_ssh.host}",
                    mark_promoted=mark_standby_promoted
                )

                verification = self._verify_switchover_result()
                result["verification"] = verification

                verification_entry = {
                    "node": "cluster",
                    "description": "验证角色状态",
                    "status": "success" if verification.get("success") else "error",
                    "timestamp": datetime.utcnow().isoformat(),
                    "former_primary_role": verification["former_primary"].get("database_role"),
                    "former_standby_role": verification["former_standby"].get("database_role"),
                }
                result["steps"].append(verification_entry)

                if not verification.get("success"):
                    raise RuntimeError("角色验证失败，未检测到预期的主备角色")

                result["status"] = "success"
                result["message"] = "切换成功，角色已互换"

            except Exception as workflow_error:
                rollback_info = self._rollback_switchover(
                    primary_committed=primary_committed,
                    standby_promoted=standby_promoted,
                    steps_log=result["steps"]
                )

                if rollback_info:
                    result["rollback"] = rollback_info

                result["status"] = "error"
                result["error"] = str(workflow_error)
                raise

        except Exception as e:
            logger.error(f"Switchover 失败: {e}")
            result["status"] = "error"
            result["error"] = str(e)

        finally:
            self.primary_ssh.close()
            self.standby_ssh.close()

        return result

    def _execute_sql_step(
        self,
        sql_executor: SqlExecutor,
        sql: str,
        description: str,
        node_label: str,
        steps_log: List[Dict[str, Any]],
        timeout: int
    ):
        """统一的 SQL 步骤执行与记录"""
        step_entry = {
            "node": node_label,
            "description": description,
            "sql": sql,
            "started_at": datetime.utcnow().isoformat(),
            "timeout": timeout,
        }

        logger.info(f"{description} (节点: {node_label})")
        steps_log.append(step_entry)

        start = time.time()
        result = sql_executor.execute_sql(sql, timeout=timeout)
        duration = round(time.time() - start, 2)
        step_entry["duration"] = duration

        if result.success:
            step_entry["status"] = "success"
            step_entry["stdout"] = result.stdout.strip()
            return result

        step_entry["status"] = "error"
        step_entry["stderr"] = result.stderr.strip()
        raise RuntimeError(f"{description} 失败: {step_entry['stderr'] or '未知错误'}")

    def _demote_database_to_standby(
        self,
        sql_executor: SqlExecutor,
        steps_log: List[Dict[str, Any]],
        node_label: str,
        mark_committed: Optional[Callable[[], None]] = None
    ):
        """将指定数据库从主库降级为备库"""
        self._execute_sql_step(
            sql_executor,
            "ALTER DATABASE COMMIT TO SWITCHOVER TO PHYSICAL STANDBY",
            f"[{node_label}] 提交为物理备库",
            node_label,
            steps_log,
            self.SWITCHOVER_TIMEOUTS["commit_to_standby"]
        )

        if mark_committed:
            mark_committed()

        self._execute_sql_step(
            sql_executor,
            "SHUTDOWN IMMEDIATE",
            f"[{node_label}] 执行 SHUTDOWN IMMEDIATE",
            node_label,
            steps_log,
            self.SWITCHOVER_TIMEOUTS["shutdown"]
        )

        self._execute_sql_step(
            sql_executor,
            "STARTUP NOMOUNT",
            f"[{node_label}] STARTUP NOMOUNT",
            node_label,
            steps_log,
            self.SWITCHOVER_TIMEOUTS["startup"]
        )

        self._execute_sql_step(
            sql_executor,
            "ALTER DATABASE MOUNT STANDBY DATABASE",
            f"[{node_label}] 挂载为备库",
            node_label,
            steps_log,
            self.SWITCHOVER_TIMEOUTS["mount"]
        )

        self._execute_sql_step(
            sql_executor,
            "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE USING CURRENT LOGFILE DISCONNECT FROM SESSION",
            f"[{node_label}] 启动实时日志应用",
            node_label,
            steps_log,
            self.SWITCHOVER_TIMEOUTS["managed_recovery"]
        )

    def _promote_database_to_primary(
        self,
        sql_executor: SqlExecutor,
        steps_log: List[Dict[str, Any]],
        node_label: str,
        mark_promoted: Optional[Callable[[], None]] = None
    ):
        """将指定数据库从备库提升为主库"""
        self._execute_sql_step(
            sql_executor,
            "ALTER DATABASE COMMIT TO SWITCHOVER TO PRIMARY WITH SESSION SHUTDOWN",
            f"[{node_label}] 提升为主库",
            node_label,
            steps_log,
            self.SWITCHOVER_TIMEOUTS["commit_to_primary"]
        )

        if mark_promoted:
            mark_promoted()

        self._execute_sql_step(
            sql_executor,
            "ALTER DATABASE OPEN",
            f"[{node_label}] 打开数据库",
            node_label,
            steps_log,
            self.SWITCHOVER_TIMEOUTS["open_database"]
        )

    def _verify_switchover_result(self) -> Dict[str, Any]:
        """验证切换后的角色状态"""
        logger.info("开始验证 Switchover 结果")
        verification = {
            "timestamp": datetime.utcnow().isoformat(),
            "former_primary": {"host": self.config.primary_ssh.host},
            "former_standby": {"host": self.config.standby_ssh.host},
            "success": False,
        }

        primary_info = self._fetch_role_info(self.primary_sql)
        standby_info = self._fetch_role_info(self.standby_sql)

        verification["former_primary"].update(primary_info)
        verification["former_standby"].update(standby_info)
        verification["success"] = (
            primary_info.get("database_role") == "PHYSICAL STANDBY"
            and standby_info.get("database_role") == "PRIMARY"
        )

        return verification

    def _fetch_role_info(self, sql_executor: SqlExecutor) -> Dict[str, Any]:
        """查询数据库角色、打开模式等信息"""
        role_sql = "SELECT database_role, open_mode, protection_mode FROM v$database"
        result = sql_executor.execute_sql(role_sql, timeout=self.SWITCHOVER_TIMEOUTS["role_check"])

        info = {
            "database_role": None,
            "open_mode": None,
            "protection_mode": None,
            "success": result.success,
        }

        if result.success:
            lines = [line.strip() for line in result.stdout.split("\n") if line.strip()]
            if len(lines) >= 3:
                info["database_role"] = lines[0]
                info["open_mode"] = lines[1]
                info["protection_mode"] = lines[2]
            elif len(lines) >= 2:
                info["database_role"] = lines[0]
                info["open_mode"] = lines[1]
            elif lines:
                info["database_role"] = lines[0]
        else:
            info["error"] = result.stderr.strip()
            logger.error(f"查询角色信息失败: {info['error']}")

        return info

    def _rollback_switchover(
        self,
        primary_committed: bool,
        standby_promoted: bool,
        steps_log: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        切换失败后的回滚逻辑：
        - 若主库已切换为备库但备库尚未提升，则仅将主库重新提升为主库
        - 若备库也已提升为主库，则按照反向切换流程恢复原角色
        """
        rollback_info = {
            "attempted": False,
            "status": "skipped",
        }

        if not primary_committed and not standby_promoted:
            return rollback_info

        rollback_info["attempted"] = True
        logger.warning("Switchover 失败，尝试回滚到原主备角色")

        # 确保 SSH 连接仍可用
        if not self.primary_ssh.client:
            if not self.primary_ssh.connect():
                rollback_info["status"] = "failed"
                rollback_info["error"] = "无法重新连接主库 SSH，回滚中止"
                return rollback_info

        if not self.standby_ssh.client:
            if not self.standby_ssh.connect():
                rollback_info["status"] = "failed"
                rollback_info["error"] = "无法重新连接备库 SSH，回滚中止"
                return rollback_info

        try:
            if standby_promoted:
                self._demote_database_to_standby(
                    sql_executor=self.standby_sql,
                    steps_log=steps_log,
                    node_label=f"rollback:new_primary@{self.config.standby_ssh.host}",
                    mark_committed=None
                )
                self._promote_database_to_primary(
                    sql_executor=self.primary_sql,
                    steps_log=steps_log,
                    node_label=f"rollback:original_primary@{self.config.primary_ssh.host}",
                    mark_promoted=None
                )
            elif primary_committed:
                self._promote_database_to_primary(
                    sql_executor=self.primary_sql,
                    steps_log=steps_log,
                    node_label=f"rollback:original_primary@{self.config.primary_ssh.host}",
                    mark_promoted=None
                )

            rollback_info["status"] = "success"

        except Exception as rollback_error:
            rollback_info["status"] = "failed"
            rollback_info["error"] = str(rollback_error)
            logger.error(f"Switchover 回滚失败: {rollback_error}")

        return rollback_info

    def _switchover_precheck(self) -> Dict[str, bool]:
        """Switchover 预检查"""
        checks = {
            "primary_connected": False,
            "standby_connected": False,
            "primary_role_ok": False,
            "standby_role_ok": False,
            "no_gap": False,
            "standby_in_sync": False,
        }

        # 连接主库
        if self.primary_ssh.connect():
            checks["primary_connected"] = True
            primary_status = self._get_primary_status()
            checks["primary_role_ok"] = primary_status.get("database_role") == "PRIMARY"
            self.primary_ssh.close()

        # 连接备库
        if self.standby_ssh.connect():
            checks["standby_connected"] = True
            standby_status = self._get_standby_status()
            checks["standby_role_ok"] = standby_status.get("database_role") == "PHYSICAL STANDBY"
            checks["standby_in_sync"] = standby_status.get("mrp_status") == "APPLYING_LOG"
            self.standby_ssh.close()

        # 检查 gap
        full_status = self.status()
        checks["no_gap"] = not full_status["sync"].get("gap", True)

        return checks

    def failover(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        执行 Failover（应急接管）

        Args:
            dry_run: 是否为演练模式

        Returns:
            接管结果
        """
        logger.info(f"执行 Failover，集群: {self.cluster_id}, 演练模式: {dry_run}")

        result = {
            "action": "failover",
            "cluster_id": self.cluster_id,
            "dry_run": dry_run,
            "status": "pending",
            "checks": {},
            "steps": [],
        }

        try:
            # 预检查
            checks = self._failover_precheck()
            result["checks"] = checks

            if not checks.get("standby_available", False):
                result["status"] = "failed"
                result["error"] = "备库不可用，无法执行接管"
                return result

            if dry_run:
                result["status"] = "dry_run_success"
                result["message"] = "演练模式：备库可用，未实际执行接管"
                return result

            steps_log = result["steps"]

            if not self.standby_ssh.connect():
                raise RuntimeError("无法连接备库 SSH，终止接管")

            node_label = f"standby@{self.config.standby_ssh.host}"

            self._perform_failover_promotion(
                sql_executor=self.standby_sql,
                steps_log=steps_log,
                node_label=node_label
            )

            failed_primary_info = self._record_failed_primary(
                steps_log=steps_log,
                reason="检测到主库不可用，标记为失效"
            )
            result["failed_primary"] = failed_primary_info

            verification = self._verify_failover_result()
            result["verification"] = verification

            verification_entry = {
                "node": "cluster",
                "description": "验证新主库状态",
                "status": "success" if verification.get("success") else "error",
                "timestamp": datetime.utcnow().isoformat(),
                "new_primary_role": verification["new_primary"].get("database_role"),
                "new_primary_open_mode": verification["new_primary"].get("open_mode"),
            }
            steps_log.append(verification_entry)

            if not verification.get("success"):
                raise RuntimeError("Failover 验证失败：未将备库成功提升为主库")

            result["status"] = "success"
            result["message"] = "接管成功，新主库已激活"

        except Exception as e:
            logger.error(f"Failover 失败: {e}")
            result["status"] = "error"
            result["error"] = str(e)

        finally:
            self.primary_ssh.close()
            self.standby_ssh.close()

        return result

    def _failover_precheck(self) -> Dict[str, bool]:
        """Failover 预检查"""
        checks = {
            "standby_connected": False,
            "standby_role_ok": False,
            "standby_available": False,
        }

        # 连接备库
        if self.standby_ssh.connect():
            checks["standby_connected"] = True
            standby_status = self._get_standby_status()
            checks["standby_role_ok"] = standby_status.get("database_role") == "PHYSICAL STANDBY"
            checks["standby_available"] = checks["standby_role_ok"]
            self.standby_ssh.close()

        return checks

    def _perform_failover_promotion(
        self,
        sql_executor: SqlExecutor,
        steps_log: List[Dict[str, Any]],
        node_label: str
    ):
        """执行备库应急接管的 SQL 步骤"""
        self._execute_sql_step(
            sql_executor,
            "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE CANCEL",
            f"[{node_label}] 停止备库恢复进程",
            node_label,
            steps_log,
            self.FAILOVER_TIMEOUTS["cancel_recovery"]
        )

        self._execute_sql_step(
            sql_executor,
            "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE FINISH",
            f"[{node_label}] 完成所有未应用的归档日志",
            node_label,
            steps_log,
            self.FAILOVER_TIMEOUTS["finish_recovery"]
        )

        self._execute_sql_step(
            sql_executor,
            "ALTER DATABASE COMMIT TO SWITCHOVER TO PRIMARY WITH SESSION SHUTDOWN",
            f"[{node_label}] 提升为主库",
            node_label,
            steps_log,
            self.FAILOVER_TIMEOUTS["commit_to_primary"]
        )

        self._execute_sql_step(
            sql_executor,
            "ALTER DATABASE OPEN",
            f"[{node_label}] 打开数据库",
            node_label,
            steps_log,
            self.FAILOVER_TIMEOUTS["open_database"]
        )

    def _record_failed_primary(
        self,
        steps_log: List[Dict[str, Any]],
        reason: str
    ) -> Dict[str, Any]:
        """记录原主库失效状态"""
        timestamp = datetime.utcnow().isoformat()
        entry = {
            "node": f"primary@{self.config.primary_ssh.host}",
            "description": "标记原主库为失效",
            "status": "warning",
            "timestamp": timestamp,
            "reason": reason,
        }
        steps_log.append(entry)

        logger.warning(f"原主库 {self.config.primary_ssh.host} 标记为失效: {reason}")
        return {
            "host": self.config.primary_ssh.host,
            "status": "failed",
            "reason": reason,
            "marked_at": timestamp,
        }

    def _verify_failover_result(self) -> Dict[str, Any]:
        """验证 Failover 是否成功"""
        logger.info("开始验证 Failover 结果")
        verification = {
            "timestamp": datetime.utcnow().isoformat(),
            "new_primary": {"host": self.config.standby_ssh.host},
            "former_primary": {
                "host": self.config.primary_ssh.host,
                "status": "failed",
            },
            "success": False,
        }

        new_primary_info = self._fetch_role_info(self.standby_sql)
        verification["new_primary"].update(new_primary_info)
        verification["success"] = new_primary_info.get("database_role") == "PRIMARY"

        return verification

    def manage(self, action: str, **kwargs) -> Any:
        """
        执行管理操作

        Args:
            action: 操作类型
                - 'sync': 手动同步（切换日志）
                - 'cleanup_archive': 清理归档日志
                - 'backup': 备份主库
                - 'recovery': 恢复备库

        Returns:
            操作结果
        """
        logger.info(f"执行管理操作: {action}, 集群: {self.cluster_id}")

        result = {
            "action": action,
            "cluster_id": self.cluster_id,
            "status": "pending"
        }

        try:
            if action == "sync":
                result = self._manual_sync(**kwargs)
            elif action == "cleanup_archive":
                result = self._cleanup_archive(**kwargs)
            elif action == "backup":
                result = self._backup_primary(**kwargs)
            elif action == "recovery":
                result = self._recovery_standby(**kwargs)
            else:
                result["status"] = "error"
                result["error"] = f"不支持的操作: {action}"

        except Exception as e:
            logger.error(f"管理操作失败: {e}")
            result["status"] = "error"
            result["error"] = str(e)

        return result

    def _manual_sync(self, **kwargs) -> Dict[str, Any]:
        """手动同步：强制切换日志"""
        result = {"action": "sync", "status": "pending"}

        if self.primary_ssh.connect():
            # 强制切换日志
            sql = "ALTER SYSTEM SWITCH LOGFILE"
            sql_result = self.primary_sql.execute_sql(sql)

            result["status"] = "success" if sql_result.success else "error"
            result["message"] = sql_result.stdout if sql_result.success else sql_result.stderr

            self.primary_ssh.close()

        return result

    def _cleanup_archive(self, **kwargs) -> Dict[str, Any]:
        """清理归档日志"""
        result = {"action": "cleanup_archive", "status": "pending"}

        logger.info(f"[{self.cluster_id}] 开始清理主库归档日志（保留 7 天）")

        if not self.primary_ssh.connect():
            error_msg = "无法连接主库 SSH，终止归档清理"
            logger.error(error_msg)
            result["status"] = "error"
            result["error"] = error_msg
            return result

        timeout = kwargs.get("timeout")

        try:
            rman_command = (
                "DELETE ARCHIVELOG UNTIL TIME 'SYSDATE-7';\n"
                "CROSSCHECK ARCHIVELOG ALL;\n"
                "DELETE EXPIRED ARCHIVELOG ALL;"
            )
            rman_result = self.primary_sql.execute_rman(rman_command, timeout=timeout)

            result["stdout"] = rman_result.stdout.strip()
            result["stderr"] = rman_result.stderr.strip()

            if rman_result.success:
                result["status"] = "success"
                result["message"] = "归档日志清理完成"
                logger.info(f"[{self.cluster_id}] 归档日志清理成功")
            else:
                error_msg = result["stderr"] or "RMAN 归档清理失败"
                result["status"] = "error"
                result["error"] = error_msg
                logger.error(f"[{self.cluster_id}] 归档日志清理失败: {error_msg}")

        except Exception as e:
            logger.error(f"[{self.cluster_id}] 执行归档清理出现异常: {e}")
            result["status"] = "error"
            result["error"] = str(e)
        finally:
            self.primary_ssh.close()

        return result

    def _backup_primary(self, **kwargs) -> Dict[str, Any]:
        """备份主库"""
        result = {"action": "backup", "status": "pending"}

        logger.info(f"[{self.cluster_id}] 开始执行主库全量备份")

        if not self.primary_ssh.connect():
            error_msg = "无法连接主库 SSH，终止备份"
            logger.error(error_msg)
            result["status"] = "error"
            result["error"] = error_msg
            return result

        timeout = kwargs.get("timeout")

        try:
            rman_command = "BACKUP DATABASE PLUS ARCHIVELOG;"
            rman_result = self.primary_sql.execute_rman(rman_command, timeout=timeout)

            result["stdout"] = rman_result.stdout.strip()
            result["stderr"] = rman_result.stderr.strip()

            if rman_result.success:
                result["status"] = "success"
                result["message"] = "主库全量备份完成"
                logger.info(f"[{self.cluster_id}] 主库备份成功")
            else:
                error_msg = result["stderr"] or "RMAN 备份执行失败"
                result["status"] = "error"
                result["error"] = error_msg
                logger.error(f"[{self.cluster_id}] 主库备份失败: {error_msg}")

        except Exception as e:
            logger.error(f"[{self.cluster_id}] 执行主库备份出现异常: {e}")
            result["status"] = "error"
            result["error"] = str(e)
        finally:
            self.primary_ssh.close()

        return result

    def _recovery_standby(self, **kwargs) -> Dict[str, Any]:
        """恢复备库"""
        result = {"action": "recovery", "status": "pending"}

        if self.standby_ssh.connect():
            # 启动实时应用
            sql = "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE USING CURRENT LOGFILE DISCONNECT FROM SESSION"
            sql_result = self.standby_sql.execute_sql(sql)

            result["status"] = "success" if sql_result.success else "error"
            result["message"] = sql_result.stdout if sql_result.success else sql_result.stderr

            self.standby_ssh.close()

        return result
