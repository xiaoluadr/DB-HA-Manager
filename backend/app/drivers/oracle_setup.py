"""Oracle Data Guard 搭建流程实现 - 阶段化任务编排版本"""

import json
import os
import shlex
import shutil
import tempfile
import uuid
import hashlib
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Callable, Tuple
from datetime import datetime
from enum import Enum
from loguru import logger

# 导入新的数据模型
from ..models.schemas import (
    SetupStepStatus,
    SetupStageStatus,
    RiskLevel,
    SetupStepDetail,
    SetupStageDetail,
    SetupTaskProgress,
)

DEFAULT_LISTENER_PORT = 1521
ALLOWED_PATH_STRATEGIES = {"same", "mirror", "convert", "custom"}
ALLOWED_REDO_STRATEGIES = {"same", "mirror", "convert", "custom"}
ALLOWED_ARCHIVE_POLICIES = {"none", "delete_input", "interval"}


class OracleSetupStage(str, Enum):
    """搭建阶段枚举"""
    COLLECT_INPUT_AND_VALIDATE = "collect_input_and_validate"
    REMOTE_DISCOVERY = "remote_discovery"
    PRECHECK = "precheck"
    GENERATE_PLAN = "generate_plan"
    PREPARE_PRIMARY = "prepare_primary"
    PREPARE_STANDBY = "prepare_standby"
    DUPLICATE_STANDBY = "duplicate_standby"
    START_MANAGED_RECOVERY = "start_managed_recovery"
    VERIFY_RESULT = "verify_result"
    FINALIZE_REPORT = "finalize_report"


class OracleSetupStep:
    """旧版步骤定义（保持向后兼容）"""
    ENV_CHECK = "环境检查"
    PARAM_CONFIG = "参数配置"
    NETWORK_CONFIG = "网络配置"
    STANDBY_PREPARE = "备库准备"
    BACKUP_TRANSFER = "备份传输"
    STANDBY_RECOVERY = "备库恢复"
    ENABLE_REALTIME = "启用实时应用"


class StagedSetupProgress:
    """阶段化搭建进度跟踪器"""

    DEFAULT_STAGE_SEQUENCE: List[OracleSetupStage] = [
        OracleSetupStage.COLLECT_INPUT_AND_VALIDATE,
        OracleSetupStage.REMOTE_DISCOVERY,
        OracleSetupStage.PRECHECK,
        OracleSetupStage.GENERATE_PLAN,
        OracleSetupStage.PREPARE_PRIMARY,
        OracleSetupStage.PREPARE_STANDBY,
        OracleSetupStage.DUPLICATE_STANDBY,
        OracleSetupStage.START_MANAGED_RECOVERY,
        OracleSetupStage.VERIFY_RESULT,
        OracleSetupStage.FINALIZE_REPORT,
    ]

    STAGE_DEFINITIONS: Dict[OracleSetupStage, Dict[str, str]] = {
        OracleSetupStage.COLLECT_INPUT_AND_VALIDATE: {
            "display_name": "收集输入并校验",
            "description": "验证搭建所需的配置信息与输入参数。",
        },
        OracleSetupStage.REMOTE_DISCOVERY: {
            "display_name": "远程环境发现",
            "description": "连接主备主机并采集现状信息。",
        },
        OracleSetupStage.PRECHECK: {
            "display_name": "运行前检查",
            "description": "检查主库状态、归档模式、磁盘空间等依赖。",
        },
        OracleSetupStage.GENERATE_PLAN: {
            "display_name": "生成执行计划",
            "description": "汇总检查结果，生成幂等执行计划。",
        },
        OracleSetupStage.PREPARE_PRIMARY: {
            "display_name": "主库准备",
            "description": "调整主库参数、准备必要文件。",
        },
        OracleSetupStage.PREPARE_STANDBY: {
            "display_name": "备库准备",
            "description": "创建备库目录及参数文件。",
        },
        OracleSetupStage.DUPLICATE_STANDBY: {
            "display_name": "备库复制",
            "description": "执行 RMAN 复制与数据同步。",
        },
        OracleSetupStage.START_MANAGED_RECOVERY: {
            "display_name": "启动 MRP",
            "description": "启动并验证备库受管恢复进程。",
        },
        OracleSetupStage.VERIFY_RESULT: {
            "display_name": "结果校验",
            "description": "验证主备同步与告警情况。",
        },
        OracleSetupStage.FINALIZE_REPORT: {
            "display_name": "生成报告",
            "description": "汇总全过程，输出最终报告。",
        },
    }

    def __init__(
        self,
        total_steps: Optional[int] = None,
        stage_sequence: Optional[List[OracleSetupStage]] = None,
        log_callback: Optional[Callable[[str, str, str, str], None]] = None,
        status_callback: Optional[Callable[[str, str, int, str], None]] = None,
        cancel_callback: Optional[Callable[[], None]] = None,
    ):
        self.stage_sequence = stage_sequence or list(self.DEFAULT_STAGE_SEQUENCE)
        self._stage_value_map = {stage.value: stage for stage in self.stage_sequence}
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.cancel_callback = cancel_callback
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        self.progress_log: List[Dict[str, Any]] = []
        self.current_step: str = ""
        self.total_steps = total_steps or len(self.stage_sequence)
        self.step_status: Dict[str, str] = {}
        self._checkpoints: Dict[str, Dict[str, Any]] = {}
        self._step_index: Dict[Tuple[str, str], SetupStepDetail] = {}

        self.task_progress = SetupTaskProgress(
            current_stage=None,
            total_stages=len(self.stage_sequence),
            completed_stages=0,
            stages=self._build_stage_map(),
            progress_percent=0,
            elapsed_seconds=0,
            overall_status=SetupStageStatus.PENDING,
        )

    def _build_stage_map(self) -> Dict[str, SetupStageDetail]:
        stage_map: Dict[str, SetupStageDetail] = {}
        for stage in self.stage_sequence:
            meta = self.STAGE_DEFINITIONS.get(stage, {})
            stage_map[stage.value] = SetupStageDetail(
                stage_name=stage.value,
                display_name=meta.get("display_name", stage.value),
                description=meta.get("description"),
                status=SetupStageStatus.PENDING,
            )
        return stage_map

    def _check_cancelled(self) -> None:
        if self.cancel_callback:
            self.cancel_callback()

    def checkpoint(self) -> None:
        """供执行流程在关键节点调用的取消检查"""
        self._check_cancelled()

    def start(self) -> None:
        """开始整体流程"""
        self._check_cancelled()
        if not self.started_at:
            self.started_at = datetime.utcnow()
            self.task_progress.overall_status = SetupStageStatus.RUNNING
            self.task_progress.elapsed_seconds = 0
            logger.info(f"开始阶段化搭建流程: {self.started_at.isoformat()}")

    def log(self, entity: str, level: str, message: str, result: str = "info") -> None:
        """统一日志出口"""
        self._check_cancelled()
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "entity": entity,
            "level": level,
            "message": message,
            "result": result,
        }
        self.progress_log.append(entry)
        log_method = getattr(logger, level.lower(), logger.info)
        log_method(f"[{entity}] {message}")
        if self.log_callback:
            try:
                self.log_callback(entity, level, message, result)
            except Exception as callback_error:
                logger.warning(f"进度日志回调失败: {callback_error}")

    def start_stage(self, stage: OracleSetupStage, description: Optional[str] = None) -> SetupStageDetail:
        """标记阶段开始"""
        self._check_cancelled()
        self.start()

        detail = self.task_progress.stages[stage.value]
        detail.started_at = datetime.utcnow()
        detail.finished_at = None
        detail.duration_seconds = None
        detail.error_message = None
        detail.status = SetupStageStatus.RUNNING
        if description:
            detail.description = description
        self.task_progress.current_stage = stage.value
        self.log(stage.value, "info", f"阶段开始: {detail.display_name}")
        self._emit_status_update(stage.value, SetupStageStatus.RUNNING.value, detail.display_name)
        return detail

    def complete_stage(
        self,
        stage: OracleSetupStage,
        status: SetupStageStatus,
        summary: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """标记阶段完成"""
        detail = self.task_progress.stages[stage.value]
        detail.finished_at = datetime.utcnow()
        if detail.started_at:
            detail.duration_seconds = (detail.finished_at - detail.started_at).total_seconds()
        detail.status = status
        detail.summary = summary
        detail.error_message = error
        if status == SetupStageStatus.SUCCESS:
            self.task_progress.completed_stages = sum(
                1
                for defined_stage in self.stage_sequence
                if self.task_progress.stages[defined_stage.value].status == SetupStageStatus.SUCCESS
            )
        elif status == SetupStageStatus.FAILED:
            self.task_progress.overall_status = SetupStageStatus.FAILED

        self._update_elapsed()
        self._recalculate_percent()

        if (
            status == SetupStageStatus.SUCCESS
            and self.task_progress.completed_stages == self.task_progress.total_stages
        ):
            self.task_progress.overall_status = SetupStageStatus.SUCCESS
            self.completed_at = detail.finished_at

        message = error or f"阶段完成: {detail.display_name}"
        level = "error" if status == SetupStageStatus.FAILED else "info"
        self.log(stage.value, level, message, result=status.value)
        self._emit_status_update(stage.value, status.value, message)

    def start_step(
        self,
        stage: OracleSetupStage,
        step_id: str,
        step_name: str,
        target_host: str,
        risk_level: RiskLevel,
        description: Optional[str] = None,
    ) -> SetupStepDetail:
        """记录阶段内部步骤开始"""
        self._check_cancelled()
        now = datetime.utcnow()
        step_detail = SetupStepDetail(
            step_id=step_id,
            step_name=step_name,
            display_name=step_name,
            description=description,
            target_host=target_host,
            status=SetupStepStatus.RUNNING,
            started_at=now,
            risk_level=risk_level,
            checkpoint_data=self._checkpoints.get(step_id),
        )
        stage_detail = self.task_progress.stages[stage.value]
        stage_detail.steps.append(step_detail)
        self._step_index[(stage.value, step_id)] = step_detail
        self.current_step = step_id
        self.update_step_status(step_id, SetupStepStatus.RUNNING, description or step_name)
        return step_detail

    def finish_step(
        self,
        stage: OracleSetupStage,
        step_id: str,
        status: SetupStepStatus,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        error_message: Optional[str] = None,
        checkpoint_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """记录步骤完成"""
        detail = self._get_step_detail(stage, step_id)
        detail.status = status
        detail.stdout = stdout
        detail.stderr = stderr
        detail.error_message = error_message
        detail.finished_at = datetime.utcnow()
        if detail.started_at:
            detail.duration_seconds = (detail.finished_at - detail.started_at).total_seconds()
        if checkpoint_data is not None:
            detail.checkpoint_data = checkpoint_data
            self.record_checkpoint(step_id, checkpoint_data)
        message = error_message or f"{step_id} -> {status}"
        level = "error" if status == SetupStepStatus.FAILED else "info"
        self.log(step_id, level, message, result=status.value)
        self.update_step_status(step_id, status, message)

    def skip_step(
        self,
        stage: OracleSetupStage,
        step_id: str,
        checkpoint_data: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> None:
        """记录步骤跳过"""
        detail = self._get_step_detail(stage, step_id)
        detail.status = SetupStepStatus.SKIPPED
        detail.finished_at = datetime.utcnow()
        detail.error_message = reason
        if checkpoint_data is not None:
            detail.checkpoint_data = checkpoint_data
            self.record_checkpoint(step_id, checkpoint_data)
        self.log(step_id, "info", reason or "由检查点跳过", result=SetupStepStatus.SKIPPED)
        self.update_step_status(step_id, SetupStepStatus.SKIPPED, reason or "已跳过")

    def record_checkpoint(self, step_id: str, data: Dict[str, Any]) -> None:
        """保存步骤检查点"""
        self._checkpoints[step_id] = data

    def get_checkpoint(self, step_id: str) -> Optional[Dict[str, Any]]:
        return self._checkpoints.get(step_id)

    def should_skip_step(
        self,
        step_id: str,
        predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> bool:
        """根据历史检查点判断是否跳过"""
        checkpoint = self._checkpoints.get(step_id)
        if not checkpoint:
            return False
        if predicate:
            try:
                return bool(predicate(checkpoint))
            except Exception as exc:
                self.log(step_id, "warning", f"检查点判定失败: {exc}", result="warning")
                return False
        return True

    def get_progress(self) -> Dict[str, Any]:
        """兼容旧接口，返回粗粒度进度"""
        self._check_cancelled()
        self._update_elapsed()
        return {
            "current_step": self.current_step,
            "step_status": self.step_status,
            "elapsed_seconds": self.task_progress.elapsed_seconds,
            "progress_percent": self.task_progress.progress_percent,
        }

    def get_progress_snapshot(self) -> SetupTaskProgress:
        """返回完整阶段状态"""
        self._update_elapsed()
        return self.task_progress

    def finalize(self, status: Optional[SetupStageStatus] = None) -> None:
        """设置整体状态并记录完成时间"""
        if status:
            self.task_progress.overall_status = status
        if status == SetupStageStatus.SUCCESS and not self.completed_at:
            self.completed_at = datetime.utcnow()
        self._update_elapsed()

    # ---- 兼容旧版 SetupProgress 的方法 ----
    def add_log(self, step: str, level: str, message: str, result: str = "info") -> None:
        self.log(step, level, message, result)

    def update_step_status(self, step: str, status: str, message: str = "") -> None:
        """兼容旧状态回调"""
        self.step_status[step] = status
        percent = self.task_progress.progress_percent
        if self.status_callback:
            try:
                self.status_callback(step, str(status), percent, message)
            except Exception as callback_error:
                logger.warning(f"进度状态回调失败: {callback_error}")

    # ---- 内部工具方法 ----
    def _emit_status_update(self, entity: str, status: str, message: str) -> None:
        if not self.status_callback:
            return
        try:
            self.status_callback(entity, status, self.task_progress.progress_percent, message)
        except Exception as callback_error:
            logger.warning(f"阶段状态回调失败: {callback_error}")

    def _update_elapsed(self) -> None:
        if self.started_at:
            now = datetime.utcnow()
            self.task_progress.elapsed_seconds = max(0.0, (now - self.started_at).total_seconds())

    def _recalculate_percent(self) -> None:
        total = self.task_progress.total_stages or 1
        completed_ratio = self.task_progress.completed_stages / total
        percent = int(min(1.0, completed_ratio) * 100)
        current = self.task_progress.current_stage
        if current and percent < 100:
            try:
                stage_index = self.stage_sequence.index(self._stage_value_map[current])
                percent = max(percent, int(stage_index / total * 100))
            except (ValueError, KeyError):
                pass
        self.task_progress.progress_percent = min(100, max(0, percent))

    def _get_step_detail(self, stage: OracleSetupStage, step_id: str) -> SetupStepDetail:
        detail = self._step_index.get((stage.value, step_id))
        if not detail:
            raise KeyError(f"未找到步骤: {stage.value}.{step_id}")
        return detail

class StageExecutionError(RuntimeError):
    """阶段执行异常"""

    def __init__(self, stage: OracleSetupStage, step_id: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.step_id = step_id
        self.message = message

    def __str__(self) -> str:
        return f"{self.stage.value}::{self.step_id} -> {self.message}"


class StepExecutionError(StageExecutionError):
    """单个步骤执行异常"""


@dataclass
class StepSpec:
    """阶段步骤定义"""
    step_id: str
    step_name: str
    target_host: str
    risk_level: RiskLevel
    action: Callable[[], Dict[str, Any]]
    description: str = ""
    allow_checkpoint_skip: bool = True
    checkpoint_predicate: Optional[Callable[[Dict[str, Any]], bool]] = None


class StagedSetupExecutor:
    """基于阶段化编排的 Oracle Data Guard 搭建执行器"""

    STAGE_SEQUENCE: List[OracleSetupStage] = list(StagedSetupProgress.DEFAULT_STAGE_SEQUENCE)

    def __init__(
        self,
        config: Dict[str, Any],
        progress_tracker: StagedSetupProgress,
        logger_instance=logger,
    ):
        self.config = config
        self.progress = progress_tracker
        self.logger = logger_instance
        self.primary_config = config.get("__primary_config__", {}) or {}
        self.standby_config = config.get("__standby_config__", {}) or {}
        self.global_config = config.get("__global_config__", {}) or {}
        self.config_summary = config.get("__config_summary__", {}) or {}
        self.strategy_warnings: List[str] = []
        self.context: Dict[str, Any] = {
            "validation": {},
            "discovery": {},
            "precheck": {},
            "plan": {},
            "artifacts": {},
        }
        if self.config_summary:
            self.context["config_summary"] = dict(self.config_summary)
        self.primary_ssh = None
        self.standby_ssh = None
        self.primary_sql = None
        self.standby_sql = None
        self._stage_handlers = {
            OracleSetupStage.COLLECT_INPUT_AND_VALIDATE: self.execute_collect_input_and_validate,
            OracleSetupStage.REMOTE_DISCOVERY: self.execute_remote_discovery,
            OracleSetupStage.PRECHECK: self.execute_precheck,
            OracleSetupStage.GENERATE_PLAN: self.execute_generate_plan,
            OracleSetupStage.PREPARE_PRIMARY: self.execute_prepare_primary,
            OracleSetupStage.PREPARE_STANDBY: self.execute_prepare_standby,
            OracleSetupStage.DUPLICATE_STANDBY: self.execute_duplicate_standby,
            OracleSetupStage.START_MANAGED_RECOVERY: self.execute_start_managed_recovery,
            OracleSetupStage.VERIFY_RESULT: self.execute_verify_result,
            OracleSetupStage.FINALIZE_REPORT: self.execute_finalize_report,
        }

    # ---------- 对外入口 ----------
    def execute_all_stages(self) -> Dict[str, Any]:
        """顺序执行所有阶段"""
        self.logger.info("开始阶段化 Data Guard 搭建流程")
        self.progress.start()
        stage_results: Dict[str, Any] = {}

        for stage in self.STAGE_SEQUENCE:
            handler = self._stage_handlers.get(stage)
            if not handler:
                continue
            result = handler()
            stage_results[stage.value] = result
            if result.get("status") == SetupStageStatus.FAILED:
                self.logger.error(f"阶段 {stage.value} 失败，终止后续执行: {result.get('error')}")
                self.progress.finalize(SetupStageStatus.FAILED)
                break
        else:
            self.progress.finalize(SetupStageStatus.SUCCESS)

        snapshot = self._snapshot_progress()
        return {
            "stages": stage_results,
            "progress": snapshot,
            "plan": self.context.get("plan"),
            "artifacts": self.context.get("artifacts"),
            "last_error": self.context.get("last_error"),
        }

    # ---------- 阶段执行 ----------
    def execute_collect_input_and_validate(self) -> Dict[str, Any]:
        specs = [
            self._spec(
                "validate_primary_config",
                "校验主库输入",
                "primary",
                RiskLevel.MEDIUM,
                self._step_validate_primary_config,
                "检查主库配置完整性",
            ),
            self._spec(
                "validate_standby_config",
                "校验备库输入",
                "standby",
                RiskLevel.MEDIUM,
                self._step_validate_standby_config,
                "检查备库配置完整性",
            ),
            self._spec(
                "validate_storage_paths",
                "校验存储路径",
                "primary",
                RiskLevel.LOW,
                self._step_validate_storage_paths,
                "确保关键路径存在且可写",
            ),
        ]
        return self._execute_stage(OracleSetupStage.COLLECT_INPUT_AND_VALIDATE, specs)

    def execute_remote_discovery(self) -> Dict[str, Any]:
        specs = [
            self._spec(
                "connect_primary_ssh",
                "连接主库 SSH",
                "primary",
                RiskLevel.MEDIUM,
                self._step_connect_primary_ssh,
                "建立到主库的远程连接",
                allow_checkpoint_skip=False,
            ),
            self._spec(
                "connect_standby_ssh",
                "连接备库 SSH",
                "standby",
                RiskLevel.MEDIUM,
                self._step_connect_standby_ssh,
                "建立到备库的远程连接",
                allow_checkpoint_skip=False,
            ),
            self._spec(
                "discover_primary_database",
                "采集主库信息",
                "primary",
                RiskLevel.LOW,
                self._step_discover_primary_database,
                "查询主库数据库状态",
            ),
            self._spec(
                "discover_standby_environment",
                "采集备库环境",
                "standby",
                RiskLevel.LOW,
                self._step_discover_standby_environment,
                "获取备库操作系统与磁盘信息",
            ),
        ]
        return self._execute_stage(OracleSetupStage.REMOTE_DISCOVERY, specs)

    def execute_precheck(self) -> Dict[str, Any]:
        specs = [
            self._spec(
                "check_archive_mode",
                "检查归档模式",
                "primary",
                RiskLevel.MEDIUM,
                self._step_check_archive_mode,
                "验证主库是否启用 ARCHIVELOG",
            ),
            self._spec(
                "check_force_logging",
                "检查 Force Logging",
                "primary",
                RiskLevel.MEDIUM,
                self._step_check_force_logging,
                "确认 force logging 状态",
            ),
            self._spec(
                "check_primary_disk",
                "检查主库磁盘空间",
                "primary",
                RiskLevel.MEDIUM,
                self._step_check_primary_disk_usage,
                "确认数据与归档目录剩余空间",
            ),
        ]
        return self._execute_stage(OracleSetupStage.PRECHECK, specs)

    def execute_generate_plan(self) -> Dict[str, Any]:
        specs = [
            self._spec(
                "generate_plan_document",
                "生成执行计划",
                "primary",
                RiskLevel.LOW,
                self._step_generate_plan_document,
                "汇总信息生成计划文档",
                allow_checkpoint_skip=False,
            ),
            self._spec(
                "generate_plan_checksum",
                "生成计划校验和",
                "primary",
                RiskLevel.LOW,
                self._step_generate_plan_checksum,
                "为计划生成幂等校验和",
                allow_checkpoint_skip=False,
            ),
            self._spec(
                "generate_plan_preview",
                "计划预览",
                "primary",
                RiskLevel.LOW,
                self._step_generate_plan_preview,
                "输出关键信息供审阅",
            ),
        ]
        return self._execute_stage(OracleSetupStage.GENERATE_PLAN, specs)

    def execute_prepare_primary(self) -> Dict[str, Any]:
        specs = [
            self._spec(
                "enable_force_logging",
                "启用 Force Logging",
                "primary",
                RiskLevel.HIGH,
                self._step_enable_force_logging,
                "确保主库开启 force logging",
            ),
            self._spec(
                "configure_log_archive_dest",
                "配置归档目的地",
                "primary",
                RiskLevel.HIGH,
                self._step_configure_log_archive_destinations,
                "设置主库 log_archive_dest_n",
            ),
            self._spec(
                "backup_parameter_file",
                "备份参数文件",
                "primary",
                RiskLevel.MEDIUM,
                self._step_backup_primary_parameter_file,
                "备份 spfile/pfile 供回滚",
            ),
        ]
        return self._execute_stage(OracleSetupStage.PREPARE_PRIMARY, specs)

    def execute_prepare_standby(self) -> Dict[str, Any]:
        specs = [
            self._spec(
                "prepare_standby_directories",
                "创建备库目录",
                "standby",
                RiskLevel.MEDIUM,
                self._step_prepare_standby_directories,
                "创建数据、归档、网络配置目录",
            ),
            self._spec(
                "generate_standby_parameter_file",
                "生成备库参数文件",
                "standby",
                RiskLevel.MEDIUM,
                self._step_generate_standby_parameter_file,
                "生成用于启动的 init/spfile",
            ),
            self._spec(
                "configure_standby_network",
                "配置备库网络",
                "standby",
                RiskLevel.MEDIUM,
                self._step_configure_standby_network,
                "生成 tnsnames 与 listener",
            ),
        ]
        return self._execute_stage(OracleSetupStage.PREPARE_STANDBY, specs)

    def execute_duplicate_standby(self) -> Dict[str, Any]:
        specs = [
            self._spec(
                "create_rman_backup",
                "生成 RMAN 备份",
                "primary",
                RiskLevel.HIGH,
                self._step_create_rman_backup,
                "执行数据库备份供复制使用",
            ),
            self._spec(
                "transfer_backup_sets",
                "传输备份文件",
                "primary",
                RiskLevel.HIGH,
                self._step_transfer_backup_sets,
                "通过 rsync/scp 将备份推送到备库",
            ),
            self._spec(
                "restore_standby_database",
                "恢复备库数据库",
                "standby",
                RiskLevel.HIGH,
                self._step_restore_standby_database,
                "执行 RMAN duplicate/restore",
            ),
        ]
        return self._execute_stage(OracleSetupStage.DUPLICATE_STANDBY, specs)

    def execute_start_managed_recovery(self) -> Dict[str, Any]:
        specs = [
            self._spec(
                "mount_standby",
                "启动并挂载备库",
                "standby",
                RiskLevel.MEDIUM,
                self._step_mount_standby_database,
                "将备库启动到 MOUNT",
            ),
            self._spec(
                "ensure_standby_redo_logs",
                "检查/创建 Standby Redo",
                "standby",
                RiskLevel.MEDIUM,
                self._step_manage_standby_redo_logs,
                "根据 auto_create_srl 配置处理 SRL",
            ),
            self._spec(
                "start_mrp",
                "启动 MRP",
                "standby",
                RiskLevel.HIGH,
                self._step_start_managed_recovery,
                "启动受管恢复进程",
            ),
            self._spec(
                "verify_mrp_started",
                "确认 MRP",
                "standby",
                RiskLevel.MEDIUM,
                self._step_verify_mrp_started,
                "查询 v$managed_standby 状态",
            ),
        ]
        return self._execute_stage(OracleSetupStage.START_MANAGED_RECOVERY, specs)

    def execute_verify_result(self) -> Dict[str, Any]:
        specs = [
            self._spec(
                "check_data_guard_sync",
                "检查同步延迟",
                "standby",
                RiskLevel.MEDIUM,
                self._step_check_data_guard_sync,
                "读取 v$dataguard_stats",
            ),
            self._spec(
                "validate_archive_gap",
                "检查 GAP",
                "standby",
                RiskLevel.MEDIUM,
                self._step_validate_archive_gap,
                "确认 V$ARCHIVE_GAP 无缺口",
            ),
        ]
        return self._execute_stage(OracleSetupStage.VERIFY_RESULT, specs)

    def execute_finalize_report(self) -> Dict[str, Any]:
        specs = [
            self._spec(
                "compile_final_report",
                "汇总执行报告",
                "primary",
                RiskLevel.LOW,
                self._step_compile_final_report,
                "生成包含阶段详情的报告",
            ),
            self._spec(
                "persist_report_artifact",
                "固化报告文件",
                "primary",
                RiskLevel.LOW,
                self._step_persist_report_artifact,
                "将报告写入文件系统",
            ),
        ]
        return self._execute_stage(OracleSetupStage.FINALIZE_REPORT, specs)

    # ---------- 阶段与步骤执行核心 ----------
    def _execute_stage(self, stage: OracleSetupStage, step_specs: List[StepSpec]) -> Dict[str, Any]:
        description = StagedSetupProgress.STAGE_DEFINITIONS.get(stage, {}).get("description")
        self.progress.start_stage(stage, description)
        stage_summary: Dict[str, Any] = {}

        for spec in step_specs:
            try:
                stage_summary[spec.step_id] = self._execute_step(stage, spec)
            except StageExecutionError as exc:
                self.logger.error(f"阶段 {stage.value} 步骤 {spec.step_id} 失败: {exc}")
                stage_summary.setdefault(spec.step_id, {"status": SetupStepStatus.FAILED, "error": str(exc)})
                self.progress.complete_stage(stage, SetupStageStatus.FAILED, summary=stage_summary, error=str(exc))
                self.context["last_error"] = str(exc)
                return {
                    "status": SetupStageStatus.FAILED,
                    "steps": stage_summary,
                    "error": str(exc),
                }

        self.progress.complete_stage(stage, SetupStageStatus.SUCCESS, summary=stage_summary)
        return {
            "status": SetupStageStatus.SUCCESS,
            "steps": stage_summary,
        }

    def _execute_step(self, stage: OracleSetupStage, spec: StepSpec) -> Dict[str, Any]:
        self.progress.checkpoint()
        self.logger.info(f"阶段 {stage.value} -> 步骤 {spec.step_id} 开始: {spec.step_name}")
        self.progress.start_step(stage, spec.step_id, spec.step_name, spec.target_host, spec.risk_level, spec.description)

        if spec.allow_checkpoint_skip and self.progress.should_skip_step(spec.step_id, spec.checkpoint_predicate):
            checkpoint = self.progress.get_checkpoint(spec.step_id)
            self.progress.skip_step(stage, spec.step_id, checkpoint, reason="checkpoint satisfied")
            return {
                "status": SetupStepStatus.SKIPPED,
                "checkpoint_data": checkpoint,
                "reason": "checkpoint satisfied",
            }

        try:
            result = spec.action()
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.progress.finish_step(
                stage,
                spec.step_id,
                SetupStepStatus.FAILED,
                stderr=message,
                error_message=message,
            )
            raise StageExecutionError(stage, spec.step_id, message) from exc

        status = result.get("status", SetupStepStatus.SUCCESS)
        stdout = self._limit_output(result.get("stdout"))
        stderr = self._limit_output(result.get("stderr"))
        checkpoint_data = result.get("checkpoint_data")

        if status == SetupStepStatus.SKIPPED:
            self.progress.skip_step(stage, spec.step_id, checkpoint_data, reason=result.get("reason"))
        elif status == SetupStepStatus.FAILED:
            error_message = result.get("error") or "步骤执行失败"
            self.progress.finish_step(
                stage,
                spec.step_id,
                SetupStepStatus.FAILED,
                stdout=stdout,
                stderr=stderr,
                error_message=error_message,
                checkpoint_data=checkpoint_data,
            )
            raise StageExecutionError(stage, spec.step_id, error_message)
        else:
            self.progress.finish_step(
                stage,
                spec.step_id,
                SetupStepStatus.SUCCESS,
                stdout=stdout,
                stderr=stderr,
                checkpoint_data=checkpoint_data,
            )

        payload = {
            "status": status,
            "stdout": stdout,
            "stderr": stderr,
            "checkpoint_data": checkpoint_data,
        }
        for key, value in result.items():
            if key not in payload:
                payload[key] = value
        return payload

    def _spec(
        self,
        step_id: str,
        step_name: str,
        target_host: str,
        risk_level: RiskLevel,
        action: Callable[[], Dict[str, Any]],
        description: str = "",
        allow_checkpoint_skip: bool = True,
        checkpoint_predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> StepSpec:
        return StepSpec(
            step_id=step_id,
            step_name=step_name,
            target_host=target_host,
            risk_level=risk_level,
            action=action,
            description=description,
            allow_checkpoint_skip=allow_checkpoint_skip,
            checkpoint_predicate=checkpoint_predicate,
        )

    # ---------- 工具方法 ----------
    def _role_value(self, role: str, key: str, default: Any = None) -> Any:
        source = self.primary_config if role == "primary" else self.standby_config
        if not source:
            return default
        value = source.get(key)
        if value is None and key in {"sid", "oracle_sid"}:
            value = source.get("sid") or source.get("oracle_sid")
        if value is None:
            return default
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or default
        return value

    def _transport_mode(self) -> str:
        mode = self.global_config.get("log_transport_mode") or "ASYNC"
        return str(mode).strip().upper() or "ASYNC"

    def _auto_create_srl_enabled(self) -> bool:
        value = self.global_config.get("auto_create_srl")
        if value is None:
            return True
        return bool(value)

    def _standby_archive_path(self) -> str:
        path = self.global_config.get("standby_archive_path")
        if isinstance(path, str) and path.strip():
            return path.strip()
        return self._require_config_value("archivelog_path")

    def _record_strategy_warning(self, message: str) -> None:
        if message not in self.strategy_warnings:
            self.strategy_warnings.append(message)
        self.progress.log("strategy", "warning", message, result="warning")

    def _transport_clause(self) -> str:
        mode = self._transport_mode()
        if mode == "SYNC":
            return "SYNC AFFIRM"
        if mode in {"FASTSYNC", "FAST_SYNC"}:
            return "SYNC NOAFFIRM"
        return "ASYNC NOAFFIRM"

    def _limit_output(self, text: Optional[str], limit: int = 400) -> Optional[str]:
        if text is None:
            return None
        stripped = text.strip()
        if len(stripped) <= limit:
            return stripped
        return stripped[: limit - 3] + "..."

    def _snapshot_progress(self) -> Dict[str, Any]:
        snapshot = self.progress.get_progress_snapshot()
        if hasattr(snapshot, "model_dump"):
            return snapshot.model_dump()
        return snapshot.dict()

    def _require_config_value(self, key: str) -> str:
        value = self.config.get(key)
        if value in (None, ""):
            raise ValueError(f"缺少必要配置: {key}")
        return str(value)

    def _get_config_value(self, key: str, default: Optional[str] = None) -> Optional[str]:
        value = self.config.get(key, default)
        return value

    def _require_remote_executor(self, executor: Any, role: str):
        if executor is None:
            raise RuntimeError(f"{role} SSH 执行器未注入")
        return executor

    def _ensure_ssh_connection(self, executor: Any, role: str) -> bool:
        remote = self._require_remote_executor(executor, role)
        if getattr(remote, "client", None):
            return True
        connected = remote.connect()
        if not connected:
            raise RuntimeError(f"{role} SSH 连接失败")
        return True

    def _require_sql_executor(self, executor: Any, role: str):
        if executor is None:
            raise RuntimeError(f"{role} SQL 执行器未注入")
        self._ensure_ssh_connection(executor.remote, role)
        return executor

    def _query_single_value(self, executor: Any, sql: str, role: str) -> str:
        result = executor.query_single_value(sql)
        if result is None:
            raise RuntimeError(f"{role} 执行 SQL 失败: {sql}")
        return str(result).strip()

    def _execute_sql(self, executor: Any, sql: str, role: str, timeout: Optional[int] = None) -> Any:
        result = executor.execute_sql(sql, timeout=timeout)
        if not result.success:
            raise RuntimeError(f"{role} 执行 SQL 失败: {result.stderr or result.stdout}")
        return result

    def _execute_rman(self, executor: Any, script: str, role: str, timeout: Optional[int] = None) -> Any:
        result = executor.execute_rman(script, timeout=timeout)
        if not result.success:
            raise RuntimeError(f"{role} 执行 RMAN 失败: {result.stderr or result.stdout}")
        return result

    def _determine_backup_dir(self) -> str:
        arch_root = self._require_config_value("archivelog_path")
        backup_dir = os.path.join(arch_root, "dg_backup")
        return backup_dir

    def _write_remote_file(self, executor: Any, remote_path: str, content: str) -> None:
        payload = f"cat <<'EOF' > {shlex.quote(remote_path)}\n{content}\nEOF"
        result = executor.execute(payload)
        if not result.success:
            raise RuntimeError(f"写入远程文件失败: {result.stderr}")

    # ---------- 阶段具体步骤 ----------
    def _step_validate_primary_config(self) -> Dict[str, Any]:
        sid = self._role_value("primary", "sid")
        oracle_home = self.primary_config.get("oracle_home") or self.config.get("primary.oracle_home")
        db_unique = self.primary_config.get("db_unique_name") or self.config.get("primary.db_unique_name")
        service_name = self.primary_config.get("service_name") or sid
        listener_port = self.primary_config.get("listener_port") or DEFAULT_LISTENER_PORT
        storage_type = self.primary_config.get("storage_type") or self.config.get("storage_type")
        is_cdb = self.primary_config.get("is_cdb")

        ssh_required = [
            "primary_ssh.host",
            "primary_ssh.username",
            "primary_ssh.port",
        ]

        missing: List[str] = []
        if not sid:
            missing.append("primary.oracle_sid")
        if not oracle_home:
            missing.append("primary.oracle_home")
        if not db_unique:
            missing.append("primary.db_unique_name")
        missing.extend([key for key in ssh_required if not self.config.get(key)])

        if missing:
            raise ValueError(f"主库配置缺失: {', '.join(missing)}")

        payload = {
            "sid": sid,
            "oracle_home": oracle_home,
            "db_unique_name": db_unique,
            "service_name": service_name,
            "listener_port": listener_port,
            "storage_type": storage_type,
            "is_cdb": is_cdb,
            "log_transport_mode": self._transport_mode(),
        }
        self.context.setdefault("validation", {})["primary"] = payload
        self.context.setdefault("config_summary", {})["primary"] = payload
        summary_text = (
            f"主库 SID={sid}, UNIQUE={db_unique}, SERVICE={service_name}, "
            f"PORT={listener_port}, Storage={storage_type or 'n/a'}, Mode={payload['log_transport_mode']}"
        )
        self.progress.log("primary_config", "info", summary_text, result="info")
        return {
            "stdout": summary_text,
            "checkpoint_data": {"validated": True, "payload": payload, "timestamp": datetime.utcnow().isoformat()},
        }

    def _step_validate_standby_config(self) -> Dict[str, Any]:
        sid = self._role_value("standby", "sid")
        oracle_home = self.standby_config.get("oracle_home") or self.config.get("standby.oracle_home")
        db_unique = self.standby_config.get("db_unique_name") or self.config.get("standby.db_unique_name")
        service_name = self.standby_config.get("service_name") or sid or self.config.get("db_name")
        listener_port = self.standby_config.get("listener_port") or DEFAULT_LISTENER_PORT
        storage_type = self.standby_config.get("storage_type") or self.config.get("storage_type")
        is_cdb = self.standby_config.get("is_cdb")
        standby_archive_path = self._standby_archive_path()

        ssh_required = [
            "standby_ssh.host",
            "standby_ssh.username",
            "standby_ssh.port",
        ]
        missing: List[str] = []
        if not sid:
            missing.append("standby.oracle_sid")
        if not oracle_home:
            missing.append("standby.oracle_home")
        if not db_unique:
            missing.append("standby.db_unique_name")
        missing.extend([key for key in ssh_required if not self.config.get(key)])
        if missing:
            raise ValueError(f"备库配置缺失: {', '.join(missing)}")

        primary_host = self._require_config_value("primary_ssh.host")
        standby_host = self._require_config_value("standby_ssh.host")
        if primary_host == standby_host:
            raise ValueError("主备主机不能相同")

        payload = {
            "sid": sid,
            "oracle_home": oracle_home,
            "db_unique_name": db_unique,
            "service_name": service_name,
            "listener_port": listener_port,
            "storage_type": storage_type,
            "is_cdb": is_cdb,
            "standby_archive_path": standby_archive_path,
        }
        payload["primary_host"] = primary_host
        payload["standby_host"] = standby_host
        self.context.setdefault("validation", {})["standby"] = payload
        self.context.setdefault("config_summary", {})["standby"] = payload
        summary_text = (
            f"备库 SID={sid}, UNIQUE={db_unique}, SERVICE={service_name}, "
            f"PORT={listener_port}, Storage={storage_type or 'n/a'}, ArchiveDir={standby_archive_path}"
        )
        self.progress.log("standby_config", "info", summary_text, result="info")
        return {
            "stdout": summary_text,
            "checkpoint_data": {"validated": True, "payload": payload, "timestamp": datetime.utcnow().isoformat()},
        }

    def _step_validate_storage_paths(self) -> Dict[str, Any]:
        data_path = os.path.abspath(self._require_config_value("data_files_path"))
        archive_path = os.path.abspath(self._require_config_value("archivelog_path"))
        standby_archive_path = os.path.abspath(self._standby_archive_path())
        normalized = {
            "data_files_path": data_path,
            "archivelog_path": archive_path,
            "standby_archive_path": standby_archive_path,
        }

        data_strategy = (self.global_config.get("data_file_path_strategy") or "same").lower()
        redo_strategy = (self.global_config.get("redo_file_path_strategy") or "same").lower()
        archive_policy = (self.global_config.get("archive_cleanup_policy") or "none").lower()
        strategies = {
            "data_file_path_strategy": data_strategy,
            "redo_file_path_strategy": redo_strategy,
            "archive_cleanup_policy": archive_policy,
        }

        if data_strategy not in ALLOWED_PATH_STRATEGIES:
            self._record_strategy_warning(f"data_file_path_strategy={data_strategy} 超出已知范围，需人工确认")
        else:
            self._record_strategy_warning(f"data_file_path_strategy={data_strategy} 暂未实现自动路径转换")

        if redo_strategy not in ALLOWED_REDO_STRATEGIES:
            self._record_strategy_warning(f"redo_file_path_strategy={redo_strategy} 超出已知范围，需人工确认")
        else:
            self._record_strategy_warning(f"redo_file_path_strategy={redo_strategy} 暂未实现自动路径转换")

        if archive_policy not in ALLOWED_ARCHIVE_POLICIES:
            self._record_strategy_warning(f"archive_cleanup_policy={archive_policy} 非默认策略，需人工介入")
        else:
            self._record_strategy_warning(f"archive_cleanup_policy={archive_policy} 尚未自动执行清理操作")

        strategies["warnings"] = list(self.strategy_warnings)
        self.context.setdefault("validation", {})["paths"] = normalized
        self.context["validation"]["strategies"] = strategies

        return {
            "stdout": f"已校验数据与归档目录 (standby_archive_path={standby_archive_path})",
            "checkpoint_data": {"paths": normalized, "strategies": strategies},
        }

    def _step_connect_primary_ssh(self) -> Dict[str, Any]:
        executor = self._require_remote_executor(self.primary_ssh, "primary")
        self._ensure_ssh_connection(executor, "primary")
        host = self._require_config_value("primary_ssh.host")
        return {
            "stdout": f"primary SSH connected: {host}",
            "checkpoint_data": {"connected": True, "host": host},
        }

    def _step_connect_standby_ssh(self) -> Dict[str, Any]:
        executor = self._require_remote_executor(self.standby_ssh, "standby")
        self._ensure_ssh_connection(executor, "standby")
        host = self._require_config_value("standby_ssh.host")
        return {
            "stdout": f"standby SSH connected: {host}",
            "checkpoint_data": {"connected": True, "host": host},
        }

    def _step_discover_primary_database(self) -> Dict[str, Any]:
        executor = self._require_sql_executor(self.primary_sql, "primary")
        db_name = self._query_single_value(executor, "SELECT name FROM v$database", "primary")
        db_unique_name = self._query_single_value(executor, "SELECT db_unique_name FROM v$database", "primary")
        log_mode = self._query_single_value(executor, "SELECT log_mode FROM v$database", "primary")
        open_mode = self._query_single_value(executor, "SELECT open_mode FROM v$database", "primary")
        force_logging = self._query_single_value(executor, "SELECT force_logging FROM v$database", "primary")

        info = {
            "db_name": db_name,
            "db_unique_name": db_unique_name,
            "log_mode": log_mode,
            "open_mode": open_mode,
            "force_logging": force_logging,
        }
        self.context["discovery"]["primary_database"] = info
        return {
            "stdout": f"{db_name}/{db_unique_name} {log_mode} {open_mode}",
            "checkpoint_data": info,
        }

    def _step_discover_standby_environment(self) -> Dict[str, Any]:
        executor = self._require_remote_executor(self.standby_ssh, "standby")
        self._ensure_ssh_connection(executor, "standby")

        hostname_result = executor.execute("hostname && uname -a")
        if not hostname_result.success:
            raise RuntimeError(f"备库主机信息查询失败: {hostname_result.stderr}")

        data_path = self._require_config_value("data_files_path")
        disk_result = executor.execute(f"df -h {shlex.quote(data_path)}")
        if not disk_result.success:
            raise RuntimeError(f"备库磁盘查询失败: {disk_result.stderr}")

        info = {
            "hostinfo": hostname_result.stdout.strip(),
            "diskinfo": disk_result.stdout.strip(),
        }
        self.context["discovery"]["standby_environment"] = info
        return {
            "stdout": "已采集备库主机与磁盘信息",
            "checkpoint_data": info,
        }

    def _step_check_archive_mode(self) -> Dict[str, Any]:
        executor = self._require_sql_executor(self.primary_sql, "primary")
        log_mode = self._query_single_value(executor, "SELECT log_mode FROM v$database", "primary")
        self.context["precheck"]["archive_mode"] = log_mode
        if log_mode.upper() != "ARCHIVELOG":
            raise RuntimeError(f"主库未启用归档模式 (当前: {log_mode})")
        return {
            "stdout": f"ARCHIVELOG 模式: {log_mode}",
            "checkpoint_data": {"archive_mode": log_mode},
        }

    def _step_check_force_logging(self) -> Dict[str, Any]:
        executor = self._require_sql_executor(self.primary_sql, "primary")
        force_logging = self._query_single_value(executor, "SELECT force_logging FROM v$database", "primary")
        self.context["precheck"]["force_logging"] = force_logging
        return {
            "stdout": f"Force Logging: {force_logging}",
            "checkpoint_data": {"force_logging": force_logging == "YES"},
        }

    def _step_check_primary_disk_usage(self) -> Dict[str, Any]:
        executor = self._require_remote_executor(self.primary_ssh, "primary")
        self._ensure_ssh_connection(executor, "primary")
        path = self._require_config_value("data_files_path")
        command = f"df -k {shlex.quote(path)} | tail -1"
        result = executor.execute(command)
        if not result.success:
            raise RuntimeError(f"磁盘查询失败: {result.stderr}")

        parts = result.stdout.strip().split()
        if len(parts) < 4:
            raise RuntimeError(f"无法解析磁盘输出: {result.stdout}")
        try:
            available_kb = int(parts[3])
        except ValueError as exc:
            raise RuntimeError(f"磁盘输出解析失败: {result.stdout}") from exc
        available_gb = round(available_kb / (1024 * 1024), 2)
        if available_gb < 5:
            raise RuntimeError(f"主库磁盘空间不足: {available_gb}GB")

        payload = {"path": path, "available_gb": available_gb}
        self.context["precheck"]["primary_disk"] = payload
        return {
            "stdout": f"{path} 可用 {available_gb} GB",
            "checkpoint_data": payload,
        }

    def _step_generate_plan_document(self) -> Dict[str, Any]:
        primary = self.context.get("discovery", {}).get("primary_database", {})
        standby_env = self.context.get("discovery", {}).get("standby_environment", {})
        plan_id = self.context["plan"].get("plan_id") or str(uuid.uuid4())
        self.context["plan"]["plan_id"] = plan_id

        plan = {
            "plan_id": plan_id,
            "generated_at": datetime.utcnow().isoformat(),
            "primary": {
                "host": self.config.get("primary_ssh.host"),
                "oracle_sid": self.primary_config.get("sid") or self.config.get("primary.oracle_sid"),
                "db_unique_name": primary.get("db_unique_name") or self.primary_config.get("db_unique_name") or self.config.get("primary.db_unique_name"),
                "service_name": self.primary_config.get("service_name"),
                "listener_port": self.primary_config.get("listener_port"),
                "storage_type": self.primary_config.get("storage_type"),
                "log_mode": primary.get("log_mode"),
            },
            "standby": {
                "host": self.config.get("standby_ssh.host"),
                "oracle_sid": self.standby_config.get("sid") or self.config.get("standby.oracle_sid"),
                "db_unique_name": self.standby_config.get("db_unique_name") or self.config.get("standby.db_unique_name"),
                "service_name": self.standby_config.get("service_name"),
                "listener_port": self.standby_config.get("listener_port"),
                "storage_type": self.standby_config.get("storage_type"),
                "environment": standby_env,
            },
            "paths": self.context.get("validation", {}).get("paths", {}),
            "duplicate": {
                "method": "rman-active",
                "backup_directory": self._determine_backup_dir(),
            },
            "transport": {
                "log_transport_mode": self._transport_mode(),
                "auto_create_srl": self._auto_create_srl_enabled(),
                "standby_archive_path": self._standby_archive_path(),
            },
            "strategies": self.context.get("validation", {}).get("strategies"),
            "warnings": list(self.strategy_warnings),
            "config_summary": self.context.get("config_summary", self.config_summary),
        }
        self.context["plan"].update(plan)
        plan_json = json.dumps(plan, ensure_ascii=False)
        return {
            "stdout": self._limit_output(plan_json, 600),
            "checkpoint_data": {"plan_id": plan_id},
            "plan": plan,
        }

    def _step_generate_plan_checksum(self) -> Dict[str, Any]:
        plan = self.context.get("plan")
        if not plan:
            raise RuntimeError("尚未生成执行计划，无法计算校验和")
        plan_json = json.dumps(plan, sort_keys=True)
        checksum = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        self.context["plan"]["checksum"] = checksum
        return {
            "stdout": f"计划校验和: {checksum[:12]}",
            "checkpoint_data": {"checksum": checksum},
        }

    def _step_generate_plan_preview(self) -> Dict[str, Any]:
        plan = self.context.get("plan")
        if not plan:
            raise RuntimeError("计划不存在，无法生成预览")
        preview_lines = [
            f"Plan ID: {plan.get('plan_id')}",
            f"Primary: {plan.get('primary', {}).get('host')} ({plan.get('primary', {}).get('db_unique_name')})",
            f"Standby: {plan.get('standby', {}).get('host')} ({plan.get('standby', {}).get('db_unique_name')})",
            f"Backup Dir: {plan.get('duplicate', {}).get('backup_directory')}",
            f"Checksum: {plan.get('checksum')}",
            f"Transport Mode: {self._transport_mode()} (auto SRL={'ON' if self._auto_create_srl_enabled() else 'OFF'})",
            f"Standby Archive: {self._standby_archive_path()}",
        ]
        if self.strategy_warnings:
            preview_lines.append("Strategy Notes: " + "; ".join(self.strategy_warnings))
        preview = "\n".join(preview_lines)
        return {
            "stdout": preview,
            "checkpoint_data": {"preview": preview},
        }

    def _step_enable_force_logging(self) -> Dict[str, Any]:
        executor = self._require_sql_executor(self.primary_sql, "primary")
        current = self._query_single_value(executor, "SELECT force_logging FROM v$database", "primary").upper()
        storage_type = self.primary_config.get("storage_type")
        is_cdb = self.primary_config.get("is_cdb")
        if current == "YES":
            message = "Force logging already enabled"
            if storage_type or is_cdb is not None:
                message += f" (storage={storage_type or 'n/a'}, is_cdb={is_cdb})"
            return {
                "status": SetupStepStatus.SKIPPED,
                "stdout": message,
                "checkpoint_data": {"force_logging": True, "storage_type": storage_type, "is_cdb": is_cdb},
                "reason": "already enabled",
            }
        self._execute_sql(executor, "ALTER DATABASE FORCE LOGGING", "primary")
        self.progress.log(
            "force_logging",
            "info",
            f"已启用 Force Logging (storage={storage_type or 'n/a'}, is_cdb={is_cdb})",
            result="info",
        )
        return {
            "stdout": "已启用 Force Logging",
            "checkpoint_data": {
                "force_logging": True,
                "storage_type": storage_type,
                "is_cdb": is_cdb,
                "timestamp": datetime.utcnow().isoformat(),
            },
        }

    def _step_configure_log_archive_destinations(self) -> Dict[str, Any]:
        executor = self._require_sql_executor(self.primary_sql, "primary")
        archive_location = self._require_config_value("archivelog_path")
        standby_service = (
            self.standby_config.get("service_name")
            or self._get_config_value("standby.service_name")
            or self._require_config_value("standby.oracle_sid")
        )
        standby_unique = (
            self.standby_config.get("db_unique_name")
            or self._get_config_value("standby.db_unique_name")
            or f"{standby_service}_STBY"
        )
        primary_unique = (
            self.primary_config.get("db_unique_name")
            or self._get_config_value("primary.db_unique_name")
            or "PRIMARY"
        )
        transport_mode = self._transport_mode()
        dest1 = f"LOCATION={archive_location} VALID_FOR=(ALL_LOGFILES,ALL_ROLES) DB_UNIQUE_NAME={primary_unique}"
        dest2 = (
            f"SERVICE={standby_service} {self._transport_clause()} "
            f"VALID_FOR=(ONLINE_LOGFILES,PRIMARY_ROLE) DB_UNIQUE_NAME={standby_unique}"
        )
        sqls = [
            f"ALTER SYSTEM SET log_archive_dest_1='{dest1}' SCOPE=BOTH",
            f"ALTER SYSTEM SET log_archive_dest_2='{dest2}' SCOPE=BOTH",
        ]
        for sql in sqls:
            self._execute_sql(executor, sql, "primary")
        payload = {
            "dest_1": dest1,
            "dest_2": dest2,
            "log_transport_mode": transport_mode,
            "primary_listener_port": self.primary_config.get("listener_port"),
            "primary_service_name": self.primary_config.get("service_name"),
        }
        self.context.setdefault("plan", {}).setdefault("transport", payload)
        self.progress.log(
            "log_transport_mode",
            "info",
            f"Log transport mode -> {transport_mode}, standby service={standby_service}",
            result="info",
        )
        return {
            "stdout": "已配置 log_archive_dest_1/2",
            "checkpoint_data": payload,
        }

    def _step_backup_primary_parameter_file(self) -> Dict[str, Any]:
        executor = self._require_remote_executor(self.primary_ssh, "primary")
        self._ensure_ssh_connection(executor, "primary")
        oracle_home = self._require_config_value("primary.oracle_home")
        oracle_sid = self._require_config_value("primary.oracle_sid")
        spfile = os.path.join(oracle_home, "dbs", f"spfile{oracle_sid}.ora")
        pfile = os.path.join(oracle_home, "dbs", f"init{oracle_sid}.ora")
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        backup_path = os.path.join(self._determine_backup_dir(), f"{oracle_sid}_spfile_{timestamp}.bak")
        cmd = (
            f"mkdir -p {shlex.quote(os.path.dirname(backup_path))} && "
            f"if [ -f {shlex.quote(spfile)} ]; then cp {shlex.quote(spfile)} {shlex.quote(backup_path)}; "
            f"elif [ -f {shlex.quote(pfile)} ]; then cp {shlex.quote(pfile)} {shlex.quote(backup_path)}; "
            f"else exit 1; fi"
        )
        result = executor.execute(cmd)
        if not result.success:
            raise RuntimeError(f"备份参数文件失败: {result.stderr}")
        return {
            "stdout": f"参数文件已备份到 {backup_path}",
            "checkpoint_data": {"backup_path": backup_path},
        }

    def _step_prepare_standby_directories(self) -> Dict[str, Any]:
        executor = self._require_remote_executor(self.standby_ssh, "standby")
        self._ensure_ssh_connection(executor, "standby")
        paths = self.context.get("validation", {}).get("paths", {})
        data_path = paths.get("data_files_path") or self._require_config_value("data_files_path")
        archive_path = paths.get("archivelog_path") or self._require_config_value("archivelog_path")
        standby_archive_path = paths.get("standby_archive_path") or self._standby_archive_path()
        oracle_home = self._require_config_value("standby.oracle_home")
        dirs = [
            data_path,
            os.path.join(data_path, "control"),
            os.path.join(data_path, "redo"),
            archive_path,
            standby_archive_path,
            os.path.join(oracle_home, "network", "admin"),
            os.path.join(oracle_home, "dbs"),
        ]
        cmd = " && ".join([f"mkdir -p {shlex.quote(path)}" for path in dirs])
        result = executor.execute(cmd)
        if not result.success:
            raise RuntimeError(f"创建目录失败: {result.stderr}")
        self.progress.log(
            "standby_directories",
            "info",
            f"使用归档目录 {archive_path}, standby_archive_path={standby_archive_path}",
            result="info",
        )
        return {
            "stdout": "备库目录已创建",
            "checkpoint_data": {
                "dirs": dirs,
                "standby_archive_path": standby_archive_path,
                "storage_type": self.standby_config.get("storage_type"),
                "is_cdb": self.standby_config.get("is_cdb"),
            },
        }

    def _step_generate_standby_parameter_file(self) -> Dict[str, Any]:
        executor = self._require_remote_executor(self.standby_ssh, "standby")
        oracle_home = self._require_config_value("standby.oracle_home")
        standby_sid = self._role_value("standby", "sid") or self._require_config_value("standby.oracle_sid")
        standby_unique = (
            self.standby_config.get("db_unique_name")
            or self._get_config_value("standby.db_unique_name")
            or f"{standby_sid}_STBY"
        )
        primary_unique = (
            self.primary_config.get("db_unique_name")
            or self._get_config_value("primary.db_unique_name")
            or "PRIMARY"
        )
        data_path = self._require_config_value("data_files_path")
        archive_path = self._standby_archive_path()
        db_name = self.context.get("discovery", {}).get("primary_database", {}).get("db_name") or self.config.get("primary.db_name") or "ORCL"

        content_lines = [
            f"DB_NAME='{db_name}'",
            f"DB_UNIQUE_NAME='{standby_unique}'",
            f"CONTROL_FILES='{data_path}/control01.ctl'",
            f"DB_RECOVERY_FILE_DEST='{archive_path}'",
            "STANDBY_FILE_MANAGEMENT='AUTO'",
            f"FAL_SERVER='{primary_unique}'",
            f"FAL_CLIENT='{standby_unique}'",
            f"LOG_ARCHIVE_CONFIG='DG_CONFIG=({primary_unique},{standby_unique})'",
            "LOG_ARCHIVE_FORMAT='standby_%t_%s_%r.arc'",
        ]
        content = "\n".join(content_lines) + "\n"
        pfile_path = os.path.join(oracle_home, "dbs", f"init{standby_sid}.ora")
        self._write_remote_file(executor, pfile_path, content)
        return {
            "stdout": f"备库参数文件生成: {pfile_path}",
            "checkpoint_data": {"pfile_path": pfile_path},
        }

    def _step_configure_standby_network(self) -> Dict[str, Any]:
        executor = self._require_remote_executor(self.standby_ssh, "standby")
        oracle_home = self._require_config_value("standby.oracle_home")
        network_dir = os.path.join(oracle_home, "network", "admin")
        executor.execute(f"mkdir -p {shlex.quote(network_dir)}")

        primary_host = self._require_config_value("primary_ssh.host")
        standby_host = self._require_config_value("standby_ssh.host")
        primary_port = int(self.primary_config.get("listener_port") or self._get_config_value("primary.listener_port", DEFAULT_LISTENER_PORT))
        standby_port = int(self.standby_config.get("listener_port") or self._get_config_value("standby.listener_port", DEFAULT_LISTENER_PORT))
        primary_service = (
            self.primary_config.get("service_name")
            or self._get_config_value("primary.service_name")
            or self._require_config_value("primary.oracle_sid")
        )
        standby_service = (
            self.standby_config.get("service_name")
            or self._get_config_value("standby.service_name")
            or self._require_config_value("standby.oracle_sid")
        )

        tns_content = f"""# Generated by DB-HA-Manager
PRIMARY_DB =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = {primary_host})(PORT = {primary_port}))
    (CONNECT_DATA =
      (SERVICE_NAME = {primary_service})
    )
  )

STANDBY_DB =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = {standby_host})(PORT = {standby_port}))
    (CONNECT_DATA =
      (SERVICE_NAME = {standby_service})
    )
  )
"""
        listener_content = f"""# Generated by DB-HA-Manager
LISTENER =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = {standby_host})(PORT = {standby_port}))
  )
SID_LIST_LISTENER =
  (SID_LIST =
    (SID_DESC =
      (GLOBAL_DBNAME = {standby_service})
      (ORACLE_HOME = {oracle_home})
      (SID_NAME = {standby_service})
    )
  )
"""
        tns_path = os.path.join(network_dir, "tnsnames.ora")
        listener_path = os.path.join(network_dir, "listener.ora")
        self._write_remote_file(executor, tns_path, tns_content)
        self._write_remote_file(executor, listener_path, listener_content)
        return {
            "stdout": f"网络配置完成: {network_dir}",
            "checkpoint_data": {"tnsnames": tns_path, "listener": listener_path},
        }

    def _step_create_rman_backup(self) -> Dict[str, Any]:
        sql_executor = self._require_sql_executor(self.primary_sql, "primary")
        ssh_executor = self._require_remote_executor(self.primary_ssh, "primary")
        backup_dir = self._determine_backup_dir()
        ensure_dir = ssh_executor.execute(f"mkdir -p {shlex.quote(backup_dir)}")
        if not ensure_dir.success:
            raise RuntimeError(f"创建备份目录失败: {ensure_dir.stderr}")
        rman_script = f"""
RUN {{
  ALLOCATE CHANNEL ch1 DEVICE TYPE DISK;
  BACKUP AS COMPRESSED BACKUPSET DATABASE FORMAT '{backup_dir}/db_%U.bkp';
  BACKUP CURRENT CONTROLFILE FORMAT '{backup_dir}/ctl_%U.bkp';
  RELEASE CHANNEL ch1;
}}
"""
        result = self._execute_rman(sql_executor, rman_script, "primary", timeout=7200)
        return {
            "stdout": self._limit_output(result.stdout, 400),
            "checkpoint_data": {"backup_dir": backup_dir, "completed_at": datetime.utcnow().isoformat()},
        }

    def _step_transfer_backup_sets(self) -> Dict[str, Any]:
        executor = self._require_remote_executor(self.primary_ssh, "primary")
        backup_dir = self._determine_backup_dir()
        standby_host = self._require_config_value("standby_ssh.host")
        standby_user = self._require_config_value("standby_ssh.username")
        cmd = (
            f"rsync -az --delete {shlex.quote(backup_dir)}/ "
            f"{standby_user}@{standby_host}:{shlex.quote(backup_dir)}/"
        )
        result = executor.execute(cmd)
        if not result.success:
            raise RuntimeError(f"备份传输失败: {result.stderr}")
        return {
            "stdout": f"备份已同步到 {standby_host}:{backup_dir}",
            "checkpoint_data": {"backup_dir": backup_dir, "target_host": standby_host},
        }

    def _step_restore_standby_database(self) -> Dict[str, Any]:
        executor = self._require_sql_executor(self.standby_sql, "standby")
        standby_unique = (
            self.standby_config.get("db_unique_name")
            or self._get_config_value("standby.db_unique_name")
            or self._require_config_value("standby.oracle_sid")
        )
        rman_script = f"""
RUN {{
  DUPLICATE TARGET DATABASE FOR STANDBY FROM ACTIVE DATABASE
    DORECOVER
    SPFILE
    SET DB_UNIQUE_NAME='{standby_unique}'
    NOFILENAMECHECK;
}}
"""
        result = self._execute_rman(executor, rman_script, "standby", timeout=7200)
        return {
            "stdout": self._limit_output(result.stdout, 400),
            "checkpoint_data": {"standby_unique_name": standby_unique},
        }

    def _step_mount_standby_database(self) -> Dict[str, Any]:
        executor = self._require_sql_executor(self.standby_sql, "standby")
        try:
            result = self._execute_sql(executor, "STARTUP MOUNT", "standby", timeout=600)
        except RuntimeError as exc:
            raise RuntimeError(f"备库启动失败: {exc}") from exc
        return {
            "stdout": self._limit_output(result.stdout, 300),
            "checkpoint_data": {"mounted": True, "timestamp": datetime.utcnow().isoformat()},
        }

    def _step_manage_standby_redo_logs(self) -> Dict[str, Any]:
        enabled = self._auto_create_srl_enabled()
        checkpoint = {"auto_create_srl": enabled}
        if not enabled:
            message = "auto_create_srl 已关闭，跳过自动创建 Standby Redo Logs"
            return {
                "status": SetupStepStatus.SKIPPED,
                "stdout": message,
                "reason": message,
                "checkpoint_data": checkpoint,
            }

        executor = self._require_sql_executor(self.standby_sql, "standby")
        existing_value = self._query_single_value(executor, "SELECT COUNT(*) FROM v$standby_log", "standby")
        try:
            existing = int(existing_value or 0)
        except ValueError:
            existing = 0
        checkpoint["existing_logs"] = existing

        if existing > 0:
            message = f"已检测到 {existing} 个 Standby Redo Logs，跳过创建"
            return {
                "stdout": message,
                "checkpoint_data": checkpoint,
            }

        primary_executor = self._require_sql_executor(self.primary_sql, "primary")
        size_value = self._query_single_value(primary_executor, "SELECT MAX(bytes) FROM v$log", "primary")
        try:
            size_mb = max(64, int(int(size_value or 0) / (1024 * 1024)) or 64)
        except (TypeError, ValueError):
            size_mb = 64

        create_sql = f"ALTER DATABASE ADD STANDBY LOGFILE SIZE {size_mb}M"
        self._execute_sql(executor, create_sql, "standby")
        checkpoint["created"] = 1
        message = f"已创建 Standby Redo Log (size {size_mb}MB)"
        self.progress.log("auto_create_srl", "info", message, result="info")
        return {
            "stdout": message,
            "checkpoint_data": checkpoint,
        }

    def _step_start_managed_recovery(self) -> Dict[str, Any]:
        executor = self._require_sql_executor(self.standby_sql, "standby")
        sql = "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE USING CURRENT LOGFILE DISCONNECT"
        result = self._execute_sql(executor, sql, "standby", timeout=300)
        return {
            "stdout": self._limit_output(result.stdout, 300),
            "checkpoint_data": {"mrp_started": True, "timestamp": datetime.utcnow().isoformat()},
        }

    def _step_verify_mrp_started(self) -> Dict[str, Any]:
        executor = self._require_sql_executor(self.standby_sql, "standby")
        result = executor.execute_sql("SELECT process || ':' || status FROM v$managed_standby WHERE process='MRP0'")
        if not result.success or "MRP0" not in result.stdout:
            raise RuntimeError("MRP0 未在 v$managed_standby 中出现")
        return {
            "stdout": result.stdout.strip(),
            "checkpoint_data": {"mrp_status": result.stdout.strip()},
        }

    def _step_check_data_guard_sync(self) -> Dict[str, Any]:
        executor = self._require_sql_executor(self.standby_sql, "standby")
        sql = "SELECT value FROM v$dataguard_stats WHERE name='apply lag'"
        lag = self._query_single_value(executor, sql, "standby")
        self.context.setdefault("verification", {})["apply_lag"] = lag
        return {
            "stdout": f"Apply Lag: {lag}",
            "checkpoint_data": {"apply_lag": lag},
        }

    def _step_validate_archive_gap(self) -> Dict[str, Any]:
        executor = self._require_sql_executor(self.standby_sql, "standby")
        gap_sql = "SELECT COUNT(*) FROM v$archive_gap"
        gap_count = int(self._query_single_value(executor, gap_sql, "standby") or "0")
        if gap_count > 0:
            raise RuntimeError(f"存在 {gap_count} 个归档 GAP")
        return {
            "stdout": "无归档 GAP",
            "checkpoint_data": {"archive_gap": 0},
        }

    def _step_compile_final_report(self) -> Dict[str, Any]:
        snapshot = self._snapshot_progress()
        report = {
            "generated_at": datetime.utcnow().isoformat(),
            "plan": self.context.get("plan"),
            "validation": self.context.get("validation"),
            "discovery": self.context.get("discovery"),
            "precheck": self.context.get("precheck"),
            "verification": self.context.get("verification"),
            "progress": snapshot,
        }
        self.context.setdefault("artifacts", {})["final_report"] = report
        report_json = json.dumps(report, ensure_ascii=False)
        return {
            "stdout": self._limit_output(report_json, 600),
            "checkpoint_data": {"report_generated": True},
        }

    def _step_persist_report_artifact(self) -> Dict[str, Any]:
        report = self.context.get("artifacts", {}).get("final_report")
        if not report:
            raise RuntimeError("尚未生成报告，无法写入文件")
        temp_file = tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            encoding="utf-8",
            suffix=".json",
            prefix="dg_setup_report_",
        )
        with temp_file as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)
            artifact_path = fp.name
        self.context["artifacts"]["final_report_path"] = artifact_path
        return {
            "stdout": f"报告已写入 {artifact_path}",
            "checkpoint_data": {"artifact_path": artifact_path},
        }









class SetupProgress:
    """搭建进度跟踪"""

    def __init__(
        self,
        total_steps: int = 7,
        log_callback: Optional[Callable[[str, str, str, str], None]] = None,
        status_callback: Optional[Callable[[str, str, int, str], None]] = None,
        cancel_callback: Optional[Callable[[], None]] = None,
    ):
        self.step_status: Dict[str, str] = {}
        self.progress_log: List[Dict[str, Any]] = []
        self.current_step: str = ""
        self.start_time: datetime = None
        self.total_steps = max(1, total_steps)
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.cancel_callback = cancel_callback

    def _check_cancelled(self):
        """触发外部取消检查"""
        if self.cancel_callback:
            self.cancel_callback()

    def checkpoint(self):
        """供执行流程在关键节点调用的取消检查"""
        self._check_cancelled()

    def start(self):
        """开始搭建"""
        self._check_cancelled()
        self.start_time = datetime.utcnow()
        logger.info(f"开始搭建流程，开始时间: {self.start_time}")

    def add_log(self, step: str, level: str, message: str, result: str = "success"):
        """添加进度日志"""
        self._check_cancelled()
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "step": step,
            "level": level,
            "message": message,
            "result": result
        }
        self.progress_log.append(log_entry)
        logger.info(f"[{step}] {level}: {message}")
        if self.log_callback:
            try:
                self.log_callback(step, level, message, result)
            except Exception as callback_error:
                logger.warning(f"进度日志回调失败: {callback_error}")

    def update_step_status(self, step: str, status: str, message: str = ""):
        """更新步骤状态"""
        self._check_cancelled()
        self.step_status[step] = status
        if message:
            self.add_log(step, "info", message, status)
        if self.status_callback:
            completed_steps = len(self.step_status)
            percent = min(100, int(completed_steps / self.total_steps * 100))
            try:
                self.status_callback(step, status, percent, message)
            except Exception as callback_error:
                logger.warning(f"进度状态回调失败: {callback_error}")

    def get_progress(self) -> Dict[str, Any]:
        """获取当前进度"""
        self._check_cancelled()
        elapsed = (datetime.utcnow() - self.start_time).total_seconds() if self.start_time else 0
        return {
            "current_step": self.current_step,
            "step_status": self.step_status,
            "elapsed_seconds": elapsed,
            "progress_percent": min(100, int(elapsed / 600 * 100)),  # 估算进度
        }


class OracleSetupExecutor:
    """Oracle Data Guard 搭建执行器"""

    STEP_SEQUENCE = [
        (OracleSetupStep.ENV_CHECK, "step1_env_check"),
        (OracleSetupStep.PARAM_CONFIG, "step2_param_config"),
        (OracleSetupStep.NETWORK_CONFIG, "step3_network_config"),
        (OracleSetupStep.STANDBY_PREPARE, "step4_standby_prepare"),
        (OracleSetupStep.BACKUP_TRANSFER, "step5_backup_transfer"),
        (OracleSetupStep.STANDBY_RECOVERY, "step6_standby_recovery"),
        (OracleSetupStep.ENABLE_REALTIME, "step7_enable_realtime"),
    ]

    def __init__(self, config: Dict[str, Any], progress: SetupProgress):
        self.config = config
        self.progress = progress

    def step1_env_check(self) -> Dict[str, Any]:
        """
        步骤1：环境检查
        - 检查主库归档模式
        - 检查 force logging 是否启用
        - 检查监听状态
        """
        self.progress.current_step = OracleSetupStep.ENV_CHECK
        self.progress.add_log(OracleSetupStep.ENV_CHECK, "info", "开始环境检查...")

        checks = {}

        try:
            # 1. 检查归档模式
            archive_mode_sql = "SELECT log_mode FROM v$database"
            result = self.primary_sql.execute_sql(archive_mode_sql)
            if result.success:
                archive_mode = result.stdout.strip()
                checks["archive_mode"] = archive_mode in ("ARCHIVELOG", "ARCHIVELOG")
                checks["archive_mode_status"] = "OK" if checks["archive_mode"] else "NO"
                self.progress.add_log(
                    OracleSetupStep.ENV_CHECK,
                    "info" if checks["archive_mode"] else "error",
                    f"归档模式: {archive_mode}",
                    "OK" if checks["archive_mode"] else "NO"
                )
            else:
                checks["archive_mode"] = "UNKNOWN"
                checks["archive_mode_status"] = "ERROR"
                self.progress.add_log(OracleSetupStep.ENV_CHECK, "error", "无法查询归档模式", "ERROR")

            # 2. 检查 force logging
            force_logging_sql = "SELECT force_logging FROM v$database"
            result = self.primary_sql.execute_sql(force_logging_sql)
            if result.success:
                force_logging = result.stdout.strip() == "YES"
                checks["force_logging"] = force_logging
                checks["force_logging_status"] = "OK" if force_logging else "NO"
                self.progress.add_log(
                    OracleSetupStep.ENV_CHECK,
                    "info" if force_logging else "warning",
                    f"Force Logging: {'ENABLED' if force_logging else 'DISABLED'}",
                    "OK" if force_logging else "WARNING"
                )
            else:
                checks["force_logging"] = "UNKNOWN"
                checks["force_logging_status"] = "ERROR"
                self.progress.add_log(OracleSetupStep.ENV_CHECK, "error", "无法查询 force logging", "ERROR")

            # 3. 检查监听状态
            listener_sql = "SELECT status FROM v$listener"
            result = self.primary_sql.execute_sql(listener_sql)
            if result.success and result.stdout.strip():
                checks["listener_status"] = "OK"
                checks["listener_status_detail"] = result.stdout.strip()
                self.progress.add_log(OracleSetupStep.ENV_CHECK, "info", "监听器状态: " + result.stdout.strip())
            else:
                checks["listener_status"] = "ERROR"
                checks["listener_status_detail"] = "监听器未运行"
                self.progress.add_log(OracleSetupStep.ENV_CHECK, "warning", "监听器未运行", "ERROR")

            # 更新步骤状态
            all_ok = all(status == "OK" for status in checks.values())
            if all_ok:
                self.progress.update_step_status(OracleSetupStep.ENV_CHECK, "completed", "环境检查完成")
            else:
                self.progress.update_step_status(OracleSetupStep.ENV_CHECK, "warning", "环境检查存在警告")

            return {
                "step": OracleSetupStep.ENV_CHECK,
                "status": "completed" if all_ok else "warning",
                "checks": checks,
                "message": "环境检查完成" if all_ok else "环境检查存在警告"
            }

        except Exception as e:
            self.progress.add_log(OracleSetupStep.ENV_CHECK, "error", f"环境检查失败: {e}", "ERROR")
            return {
                "step": OracleSetupStep.ENV_CHECK,
                "status": "error",
                "error": str(e)
            }

    def step2_param_config(self) -> Dict[str, Any]:
        """
        步骤2：参数配置
        - 配置主库参数
        - 设置 db_name, db_unique_name
        - 设置 log_archive_dest_n
        """
        self.progress.current_step = OracleSetupStep.PARAM_CONFIG
        self.progress.add_log(OracleSetupStep.PARAM_CONFIG, "info", "开始参数配置...")

        try:
            # 1. 设置 db_name
            db_name_sql = "ALTER SYSTEM SET db_name = 'DG'"
            result = self.primary_sql.execute_sql(db_name_sql)
            self.progress.add_log(OracleSetupStep.PARAM_CONFIG, "info", "设置 db_name: " + ("OK" if result.success else "ERROR"))
            if not result.success:
                raise Exception(f"设置 db_name 失败: {result.stderr}")

            # 2. 设置 db_unique_name
            db_unique_sql = "ALTER SYSTEM SET db_unique_name = 'DG'"
            result = self.primary_sql.execute_sql(db_unique_sql)
            self.progress.add_log(OracleSetupStep.PARAM_CONFIG, "info", "设置 db_unique_name: " + ("OK" if result.success else "ERROR"))
            if not result.success:
                raise Exception(f"设置 db_unique_name 失败: {result.stderr}")

            # 3. 设置 log_archive_dest_n
            archive_dest = self.config.get("archivelog_path", "/u01/oradata/ORCL/archivelog")
            archive_dest_sql = f"ALTER SYSTEM SET log_archive_dest_n = '{archive_dest}'"
            result = self.primary_sql.execute_sql(archive_dest_sql)
            self.progress.add_log(OracleSetupStep.PARAM_CONFIG, "info", f"设置归档路径: {archive_dest} - " + ("OK" if result.success else "ERROR"))
            if not result.success:
                raise Exception(f"设置 log_archive_dest_n 失败: {result.stderr}")

            # 4. 设置 standby_file_management
            standby_file_sql = "ALTER SYSTEM SET standby_file_management = 'AUTO'"
            result = self.primary_sql.execute_sql(standby_file_sql)
            self.progress.add_log(OracleSetupStep.PARAM_CONFIG, "info", f"设置 standby_file_management: AUTO - {'OK' if result.success else 'ERROR'}")
            if not result.success:
                raise Exception(f"设置 standby_file_management 失败: {result.stderr}")

            self.progress.update_step_status(OracleSetupStep.PARAM_CONFIG, "completed", "参数配置完成")

            return {
                "step": OracleSetupStep.PARAM_CONFIG,
                "status": "completed",
                "message": "参数配置完成"
            }

        except Exception as e:
            self.progress.add_log(OracleSetupStep.PARAM_CONFIG, "error", f"参数配置失败: {e}", "ERROR")
            return {
                "step": OracleSetupStep.PARAM_CONFIG,
                "status": "error",
                "error": str(e)
            }

    def step3_network_config(self) -> Dict[str, Any]:
        """
        步骤3：网络配置
        - 配置 tnsnames.ora
        - 配置 listener.ora
        """
        self.progress.current_step = OracleSetupStep.NETWORK_CONFIG
        self.progress.add_log(OracleSetupStep.NETWORK_CONFIG, "info", "开始网络配置...")

        try:
            # 配置参数
            db_name = self.config.get("primary.oracle_sid", "ORCL")
            hostname = self.config.get("primary_ssh.host")
            port = self.config.get("primary_port", 1521)

            # TNSNAMES.ORA 配置
            tnsnames_content = f"""# Oracle Net Services Names Configuration
# Generated by DB-HA-Manager
{db_name} =
  (DESCRIPTION =
  (ADDRESS = (PROTOCOL = TCP)(HOST = {hostname})(PORT = {port}))
  (CONNECT_DATA =
    (SERVER = DEDICATED)
    (SERVICE_NAME = {db_name})
  )
"""

            # 写入 tnsnames.ora
            tnsnames_path = f"{self.config.get('primary.oracle_home', '')}/network/admin/tnsnames.ora"
            write_cmd = f'echo "{tnsnames_content}" > {tnsnames_path}'
            result = self.primary_ssh.execute(write_cmd)
            self.progress.add_log(OracleSetupStep.NETWORK_CONFIG, "info", f"写入 tnsnames.ora: " + ("OK" if result.success else "ERROR"))

            # LISTENER.ORA 配置
            listener_content = f"""# Oracle Net Services Listener Configuration
# Generated by DB-HA-Manager
LISTENER =
  (DESCRIPTION = {db_name} listener)
  (ADDRESS = (PROTOCOL = TCP)(HOST = {hostname})(PORT = {port}))
  (SID_NAME = {db_name})
  """

            listener_path = f"{self.config.get('primary.oracle_home', '')}/network/admin/listener.ora"
            write_cmd = f'echo "{listener_content}" > {listener_path}'
            result = self.primary_ssh.execute(write_cmd)
            self.progress.add_log(OracleSetupStep.NETWORK_CONFIG, "info", f"写入 listener.ora: " + ("OK" if result.success else "ERROR"))

            # 重启监听器
            restart_sql = "ALTER SYSTEM SET register=TRUE"
            result = self.primary_sql.execute_sql(restart_sql)
            self.progress.add_log(OracleSetupStep.NETWORK_CONFIG, "info", "重启监听器: " + ("OK" if result.success else "ERROR"))

            self.progress.update_step_status(OracleSetupStep.NETWORK_CONFIG, "completed", "网络配置完成")

            return {
                "step": OracleSetupStep.NETWORK_CONFIG,
                "status": "completed",
                "message": "网络配置完成"
            }

        except Exception as e:
            self.progress.add_log(OracleSetupStep.NETWORK_CONFIG, "error", f"网络配置失败: {e}", "ERROR")
            return {
                "step": OracleSetupStep.NETWORK_CONFIG,
                "status": "error",
                "error": str(e)
            }

    def step4_standby_prepare(self) -> Dict[str, Any]:
        """
        步骤4：备库准备
        - 创建目录结构
        - 配置参数文件
        """
        self.progress.current_step = OracleSetupStep.STANDBY_PREPARE
        self.progress.add_log(OracleSetupStep.STANDBY_PREPARE, "info", "开始备库准备...")

        try:
            standby_home = self.config.get("standby.oracle_home", "")
            oracle_sid = self.config.get("standby.oracle_sid", "ORCL")
            data_files_path = self.config.get("data_files_path", "")
            archive_path = self.config.get("archivelog_path", "")

            # 创建目录结构
            dirs_to_create = [
                f"{data_files_path}",
                f"{data_files_path}/system",
                f"{data_files_path}/sysaux",
                f"{archive_path}",
                f"{archive_path}/archived_logs",
                f"{standby_home}/admin",
                f"{standby_home}/network/admin"
            ]

            for dir_path in dirs_to_create:
                mkdir_cmd = f"mkdir -p {dir_path}"
                result = self.standby_ssh.execute(mkdir_cmd)
                self.progress.add_log(
                    OracleSetupStep.STANDBY_PREPARE,
                    "info",
                    f"创建目录 {dir_path}: " + ("OK" if result.success else "ERROR")
                )

            # 创建参数文件 pfile
            pfile_path = f"{standby_home}/dbs/{oracle_sid}.pfile"
            pfile_content = f"""*.db_name='DG'
*.db_unique_name='DG'
*.db_domain='DG'
*.db_domain_name='DG'
*.log_archive_format='{archive_path}/archived_logs/%t_%s_%r.arc'
"""

            write_cmd = f'echo "{pfile_content}" > {pfile_path}'
            result = self.standby_ssh.execute(write_cmd)
            self.progress.add_log(
                OracleSetupStep.STANDBY_PREPARE,
                "info",
                f"创建 pfile: {pfile_path} - " + ("OK" if result.success else "ERROR")
            )

            # 配置 standby init 参数文件
            init_path = f"{standby_home}/dbs/{oracle_sid}.ora"
            init_content = """*.db_name='DG'
*.db_unique_name='DG'
*.db_domain='DG'
*.db_domain_name='DG'
*.compatible='11.2'
*.db_block_size=8192
*.db_files='{data_files_path}'
*.diagnostic_dest={archive_path}
*.background_dump_dest={archive_path}
*.control_files='{standby_home}/dbs'
*.sga_target=false
*.sga_trace_level=off
"""

            write_cmd = f'echo "{init_content}" > {init_path}'
            result = self.standby_ssh.execute(write_cmd)
            self.progress.add_log(
                OracleSetupStep.STANDBY_PREPARE,
                    "info",
                    f"创建 init.ora: {init_path} - " + ("OK" if result.success else "ERROR")
            )

            self.progress.update_step_status(OracleSetupStep.STANDBY_PREPARE, "completed", "备库准备完成")

            return {
                "step": OracleSetupStep.STANDBY_PREPARE,
                "status": "completed",
                "message": "备库准备完成"
            }

        except Exception as e:
            self.progress.add_log(OracleSetupStep.STANDBY_PREPARE, "error", f"备库准备失败: {e}", "ERROR")
            return {
                "step": OracleSetupStep.STANDBY_PREPARE,
                "status": "error",
                "error": str(e)
            }

    def step5_backup_transfer(self) -> Dict[str, Any]:
        """
        步骤5：备份传输
        - 使用 RMAN 备份主库
        - 传输备份文件到备库
        """
        self.progress.current_step = OracleSetupStep.BACKUP_TRANSFER
        self.progress.add_log(OracleSetupStep.BACKUP_TRANSFER, "info", "开始备份传输...")

        temp_dir = tempfile.mkdtemp(prefix="dg_backup_")

        try:
            # 1. 使用 RMAN 备份主库
            backup_dir = self.config.get("backup_path", "/u01/backup")
            rman_script = f"""
RUN {{
  ALLOCATE CHANNEL ch1 TYPE DISK;
  BACKUP DATABASE FORMAT '{backup_dir}/%d_%t_%s_%p.bkp';
  BACKUP ARCHIVELOG ALL FORMAT '{backup_dir}/arch_%d_%t_%s_%p.arc' DELETE INPUT;
  BACKUP CURRENT CONTROLFILE FORMAT '{backup_dir}/control_%d_%t_%s_%p.ctl';
  RELEASE CHANNEL ch1;
}}
"""

            rman_cmd = f'rman target / log={backup_dir}/backup.log <<EOF\n{rman_script}\nEOF'
            self.progress.add_log(OracleSetupStep.BACKUP_TRANSFER, "info", f"RMAN 备份命令: {rman_cmd[:100]}...")

            # 执行备份
            result = self.primary_ssh.execute(rman_cmd)
            if not result.success:
                raise Exception(f"RMAN 备份失败: {result.stderr}")
            self.progress.add_log(OracleSetupStep.BACKUP_TRANSFER, "info", "RMAN 备份完成")

            # 2. 创建参数文件备份
            backup_pfile_cmd = "CREATE PFILE='/tmp/pfile.ora' FROM SPFILE;"
            result = self.primary_sql.execute_sql(backup_pfile_cmd)
            if not result.success:
                self.progress.add_log(OracleSetupStep.BACKUP_TRANSFER, "warning", "参数文件备份失败，使用默认配置")
            else:
                remote_pfile_path = "/tmp/pfile.ora"
                local_pfile_path = os.path.join(temp_dir, "pfile.ora")
                download_ok = self.primary_ssh.download_file(remote_pfile_path, local_pfile_path)
                upload_ok = False
                if download_ok:
                    upload_ok = self.standby_ssh.upload_file(local_pfile_path, remote_pfile_path)
                self.progress.add_log(
                    OracleSetupStep.BACKUP_TRANSFER,
                    "info",
                    f"参数文件传输: {'OK' if download_ok and upload_ok else 'ERROR'}"
                )

            # 3. 传输备份文件到备库
            quoted_backup_dir = shlex.quote(backup_dir)
            list_cmd = f"find {quoted_backup_dir} -maxdepth 1 -type f -print"
            list_result = self.primary_ssh.execute(list_cmd)
            if not list_result.success:
                raise Exception(f"查询备份文件失败: {list_result.stderr}")

            backup_files = [line.strip() for line in list_result.stdout.splitlines() if line.strip()]
            if not backup_files:
                raise Exception("未找到可传输的备份文件")

            mkdir_result = self.standby_ssh.execute(f"mkdir -p {quoted_backup_dir}")
            if not mkdir_result.success:
                raise Exception(f"备库创建备份目录失败: {mkdir_result.stderr}")

            self.progress.add_log(
                OracleSetupStep.BACKUP_TRANSFER,
                "info",
                f"开始传输备份文件，共 {len(backup_files)} 个"
            )

            for file_path in backup_files:
                filename = os.path.basename(file_path)
                local_file = os.path.join(temp_dir, filename)
                if not self.primary_ssh.download_file(file_path, local_file):
                    raise Exception(f"下载备份文件失败: {file_path}")
                remote_target = os.path.join(backup_dir, filename)
                if not self.standby_ssh.upload_file(local_file, remote_target):
                    raise Exception(f"上传备份文件失败: {filename}")
                self.progress.add_log(
                    OracleSetupStep.BACKUP_TRANSFER,
                    "info",
                    f"传输文件 {filename}: OK"
                )

            self.progress.add_log(OracleSetupStep.BACKUP_TRANSFER, "info", "备份文件传输完成")
            self.progress.update_step_status(OracleSetupStep.BACKUP_TRANSFER, "completed", "备份传输完成")
            return {
                "step": OracleSetupStep.BACKUP_TRANSFER,
                "status": "completed",
                "message": "备份传输完成"
            }

        except Exception as e:
            self.progress.add_log(OracleSetupStep.BACKUP_TRANSFER, "error", f"备份传输失败: {e}", "ERROR")
            return {
                "step": OracleSetupStep.BACKUP_TRANSFER,
                "status": "error",
                "error": str(e)
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def step6_standby_recovery(self) -> Dict[str, Any]:
        """
        步骤6：备库恢复
        - 使用 RMAN 恢复备库
        - 恢复归档日志
        """
        self.progress.current_step = OracleSetupStep.STANDBY_RECOVERY
        self.progress.add_log(OracleSetupStep.STANDBY_RECOVERY, "info", "开始备库恢复...")

        try:
            backup_dir = self.config.get("backup_path", "/u01/backup")
            dbid = self.config.get("dbid", "0")

            # 1. 使用 RMAN 恢复控制文件
            rman_restore_control = f"""
RUN {{
  SET DBID={dbid};
  STARTUP NOMOUNT;
  RESTORE CONTROLFILE FROM '{backup_dir}/control_*_*.ctl';
  ALTER DATABASE MOUNT;
}}
"""
            self.progress.add_log(OracleSetupStep.STANDBY_RECOVERY, "info", f"RMAN 恢复控制文件")
            result = self.standby_sql.execute_rman(rman_restore_control)
            if not result.success:
                raise Exception(f"恢复控制文件失败: {result.stderr}")
            self.progress.add_log(OracleSetupStep.STANDBY_RECOVERY, "info", "控制文件恢复完成")

            # 2. 使用 RMAN 恢复数据文件
            rman_restore_db = f"""
RUN {{
  ALLOCATE CHANNEL ch1 TYPE DISK;
  RESTORE DATABASE;
  RECOVER DATABASE;
  RELEASE CHANNEL ch1;
}}
"""
            self.progress.add_log(OracleSetupStep.STANDBY_RECOVERY, "info", f"RMAN 恢复数据文件")
            result = self.standby_sql.execute_rman(rman_restore_db)
            if not result.success:
                raise Exception(f"恢复数据文件失败: {result.stderr}")
            self.progress.add_log(OracleSetupStep.STANDBY_RECOVERY, "info", "数据文件恢复完成")

            # 3. 将备库设置为只读模式（用于验证）
            sql_cmd = "ALTER DATABASE OPEN READ ONLY;"
            result = self.standby_sql.execute_sql(sql_cmd)
            self.progress.add_log(OracleSetupStep.STANDBY_RECOVERY, "info", "备库已设置为只读模式")

            self.progress.update_step_status(OracleSetupStep.STANDBY_RECOVERY, "completed", "备库恢复完成")

            return {
                "step": OracleSetupStep.STANDBY_RECOVERY,
                "status": "completed",
                "message": "备库恢复完成"
            }

        except Exception as e:
            self.progress.add_log(OracleSetupStep.STANDBY_RECOVERY, "error", f"备库恢复失败: {e}", "ERROR")
            return {
                "step": OracleSetupStep.STANDBY_RECOVERY,
                "status": "error",
                "error": str(e)
            }

    def step7_enable_realtime(self) -> Dict[str, Any]:
        """
        步骤7：启用实时应用
        - 将备库设置为 MOUNT 模式
        - 启用实时应用
        - 验证同步状态
        """
        self.progress.current_step = OracleSetupStep.ENABLE_REALTIME
        self.progress.add_log(OracleSetupStep.ENABLE_REALTIME, "info", "开始启用实时应用...")

        try:
            # 1. 将备库关闭后重新启动为 MOUNT 模式
            shutdown_cmd = "SHUTDOWN IMMEDIATE"
            result = self.standby_sql.execute_sql(shutdown_cmd)
            self.progress.add_log(
                OracleSetupStep.ENABLE_REALTIME,
                "info",
                f"备库关闭命令执行: {'OK' if result.success else 'ERROR'}"
            )

            # 重置连接，避免 SHUTDOWN 后的 SQL*Plus 会话异常
            self.standby_ssh.close()
            if not self.standby_ssh.connect():
                raise Exception("备库 SSH 重连失败，无法继续启用实时应用")
            self.progress.add_log(OracleSetupStep.ENABLE_REALTIME, "info", "备库 SSH 会话已重新建立")

            # 启动为 MOUNT 模式
            startup_cmd = "STARTUP MOUNT"
            result = self.standby_sql.execute_sql(startup_cmd)
            if not result.success:
                raise Exception(f"启动备库 MOUNT 模式失败: {result.stderr}")
            self.progress.add_log(OracleSetupStep.ENABLE_REALTIME, "info", "备库已启动为 MOUNT 模式")

            # 2. 启用实时应用
            enable_apply_cmd = """
ALTER DATABASE RECOVER MANAGED STANDBY DATABASE USING CURRENT LOGFILE DISCONNECT FROM SESSION;
"""
            result = self.standby_sql.execute_sql(enable_apply_cmd)
            if not result.success:
                raise Exception(f"启用实时应用失败: {result.stderr}")
            self.progress.add_log(OracleSetupStep.ENABLE_REALTIME, "info", "实时应用已启用")

            # 3. 验证 MRP 进程状态
            check_mrp_sql = """
SELECT process, status, thread#, sequence#
FROM v$managed_standby
WHERE process LIKE 'MRP%';
"""
            result = self.standby_sql.execute_sql(check_mrp_sql)
            if result.success:
                self.progress.add_log(OracleSetupStep.ENABLE_REALTIME, "info", f"MRP 进程: {result.stdout}")
            else:
                self.progress.add_log(OracleSetupStep.ENABLE_REALTIME, "warning", "无法查询 MRP 进程状态")

            # 4. 验证同步状态
            check_lag_sql = """
SELECT name, value
FROM v$dataguard_stats
WHERE name = 'apply lag';
"""
            result = self.standby_sql.execute_sql(check_lag_sql)
            if result.success:
                self.progress.add_log(OracleSetupStep.ENABLE_REALTIME, "info", f"应用延迟: {result.stdout}")

            self.progress.update_step_status(OracleSetupStep.ENABLE_REALTIME, "completed", "实时应用已启用")

            return {
                "step": OracleSetupStep.ENABLE_REALTIME,
                "status": "completed",
                "message": "实时应用已启用"
            }

        except Exception as e:
            self.progress.add_log(OracleSetupStep.ENABLE_REALTIME, "error", f"启用实时应用失败: {e}", "ERROR")
            return {
                "step": OracleSetupStep.ENABLE_REALTIME,
                "status": "error",
                "error": str(e)
            }

    def generate_setup_commands(self) -> Dict[str, List[Dict[str, str]]]:
        """
        生成所有搭建步骤的命令供前端审批

        Returns:
            Dict[str, List[Dict]]]: 每个步骤的命令列表
                {
                    "step_name": [
                        {"command": "具体命令", "description": "命令说明", "type": "sql|ssh|rman"}
                    ]
                }
        """
        db_name = self.config.get("primary.oracle_sid", "ORCL")
        hostname = self.config.get("primary_ssh.host", "")
        port = self.config.get("primary_port", 1521)
        archive_dest = self.config.get("archivelog_path", "/u01/oradata/ORCL/archivelog")
        standby_home = self.config.get("standby.oracle_home", "")
        oracle_sid = self.config.get("standby.oracle_sid", "ORCL")
        data_files_path = self.config.get("data_files_path", "")
        archive_path = self.config.get("archivelog_path", "")
        backup_dir = self.config.get("backup_path", "/u01/backup")
        standby_host = self.config.get("standby_ssh.host", "")
        dbid = self.config.get("dbid", "0")

        commands = {
            OracleSetupStep.ENV_CHECK: [
                {
                    "command": "SELECT log_mode FROM v$database",
                    "description": "检查主库归档模式",
                    "type": "sql",
                    "target": "primary"
                },
                {
                    "command": "SELECT force_logging FROM v$database",
                    "description": "检查 Force Logging 状态",
                    "type": "sql",
                    "target": "primary"
                },
                {
                    "command": "SELECT status FROM v$listener",
                    "description": "检查监听器状态",
                    "type": "sql",
                    "target": "primary"
                }
            ],
            OracleSetupStep.PARAM_CONFIG: [
                {
                    "command": "ALTER SYSTEM SET db_name = 'DG'",
                    "description": "设置数据库标识名称",
                    "type": "sql",
                    "target": "primary"
                },
                {
                    "command": "ALTER SYSTEM SET db_unique_name = 'DG'",
                    "description": "设置数据库唯一名称",
                    "type": "sql",
                    "target": "primary"
                },
                {
                    "command": f"ALTER SYSTEM SET log_archive_dest_n = '{archive_dest}'",
                    "description": "设置归档目标路径",
                    "type": "sql",
                    "target": "primary"
                },
                {
                    "command": "ALTER SYSTEM SET standby_file_management = 'AUTO'",
                    "description": "设置备库文件管理为自动",
                    "type": "sql",
                    "target": "primary"
                }
            ],
            OracleSetupStep.NETWORK_CONFIG: [
                {
                    "command": f"echo '{db_name} =\n  (DESCRIPTION =\n  (ADDRESS = (PROTOCOL = TCP)(HOST = {hostname})(PORT = {port}))\n  (CONNECT_DATA =\n    (SERVER = DEDICATED)\n    (SERVICE_NAME = {db_name})\n  )\n)' > {self.config.get('primary.oracle_home', '')}/network/admin/tnsnames.ora",
                    "description": "配置 TNSNAMES.ORA",
                    "type": "ssh",
                    "target": "primary"
                },
                {
                    "command": f"echo 'LISTENER =\n  (DESCRIPTION = {db_name} listener)\n  (ADDRESS = (PROTOCOL = TCP)(HOST = {hostname})(PORT = {port}))\n  (SID_NAME = {db_name})\n' > {self.config.get('primary.oracle_home', '')}/network/admin/listener.ora",
                    "description": "配置 LISTENER.ORA",
                    "type": "ssh",
                    "target": "primary"
                },
                {
                    "command": "ALTER SYSTEM SET register=TRUE",
                    "description": "重启监听器注册",
                    "type": "sql",
                    "target": "primary"
                }
            ],
            OracleSetupStep.STANDBY_PREPARE: [
                {
                    "command": f"mkdir -p {data_files_path} {data_files_path}/system {data_files_path}/sysaux {archive_path} {archive_path}/archived_logs {standby_home}/admin {standby_home}/network/admin",
                    "description": "创建备库目录结构",
                    "type": "ssh",
                    "target": "standby"
                },
                {
                    "command": f"echo '*.db_name=\\'DG\\'\n*.db_unique_name=\\'DG\\'\n*.log_archive_format=\\'{archive_path}/archived_logs/%t_%s_%r.arc\\'' > {standby_home}/dbs/{oracle_sid}.pfile",
                    "description": "创建备库参数文件",
                    "type": "ssh",
                    "target": "standby"
                }
            ],
            OracleSetupStep.BACKUP_TRANSFER: [
                {
                    "command": f"rman target / log={backup_dir}/backup.log <<EOF\nRUN {{\n  ALLOCATE CHANNEL ch1 TYPE DISK;\n  BACKUP DATABASE FORMAT '{backup_dir}/%d_%t_%s_%p.bkp';\n  BACKUP ARCHIVELOG ALL FORMAT '{backup_dir}/arch_%d_%t_%s_%p.arc' DELETE INPUT;\n  BACKUP CURRENT CONTROLFILE FORMAT '{backup_dir}/control_%d_%t_%s_%p.ctl';\n  RELEASE CHANNEL ch1;\n}}\nEOF",
                    "description": "RMAN 备份主库（数据库+归档+控制文件）",
                    "type": "rman",
                    "target": "primary"
                },
                {
                    "command": f"SFTP upload /tmp/pfile.ora -> {standby_host}:/tmp/pfile.ora",
                    "description": "传输参数文件到备库",
                    "type": "ssh",
                    "target": "primary"
                },
                {
                    "command": f"SFTP upload {backup_dir}/* -> {standby_host}:{backup_dir}/",
                    "description": "通过管理端上传备份文件到备库",
                    "type": "ssh",
                    "target": "primary"
                }
            ],
            OracleSetupStep.STANDBY_RECOVERY: [
                {
                    "command": f"RUN {{\n  SET DBID={dbid};\n  STARTUP NOMOUNT;\n  RESTORE CONTROLFILE FROM '{backup_dir}/control_*_*.ctl';\n  ALTER DATABASE MOUNT;\n}}",
                    "description": "RMAN 恢复控制文件",
                    "type": "rman",
                    "target": "standby"
                },
                {
                    "command": "RUN {\n  ALLOCATE CHANNEL ch1 TYPE DISK;\n  RESTORE DATABASE;\n  RECOVER DATABASE;\n  RELEASE CHANNEL ch1;\n}",
                    "description": "RMAN 恢复数据文件和归档日志",
                    "type": "rman",
                    "target": "standby"
                },
                {
                    "command": "ALTER DATABASE OPEN READ ONLY",
                    "description": "将备库设置为只读模式（验证用）",
                    "type": "sql",
                    "target": "standby"
                }
            ],
            OracleSetupStep.ENABLE_REALTIME: [
                {
                    "command": "SHUTDOWN IMMEDIATE",
                    "description": "关闭备库",
                    "type": "sql",
                    "target": "standby"
                },
                {
                    "command": "STARTUP MOUNT",
                    "description": "启动备库为 MOUNT 模式",
                    "type": "sql",
                    "target": "standby"
                },
                {
                    "command": "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE USING CURRENT LOGFILE DISCONNECT FROM SESSION;",
                    "description": "启用实时应用",
                    "type": "sql",
                    "target": "standby"
                },
                {
                    "command": "SELECT process, status, thread#, sequence# FROM v$managed_standby WHERE process LIKE 'MRP%';",
                    "description": "验证 MRP 进程状态",
                    "type": "sql",
                    "target": "standby"
                },
                {
                    "command": "SELECT name, value FROM v$dataguard_stats WHERE name = 'apply lag';",
                    "description": "验证同步延迟",
                    "type": "sql",
                    "target": "standby"
                }
            ]
        }

        return commands

    def execute_all_steps(self, skip_steps: List[str] = None) -> Dict[str, Any]:
        """
        执行所有搭建步骤

        Args:
            skip_steps: 跳过的步骤名称列表

        Returns:
            Dict[str, Any]: 执行结果汇总
        """
        skip_steps = skip_steps or []
        results = {}

        for step_name, method_name in self.STEP_SEQUENCE:
            self.progress.checkpoint()
            step_func = getattr(self, method_name)
            if step_name in skip_steps:
                self.progress.update_step_status(step_name, "skipped", "步骤被跳过")
                results[step_name] = {"status": "skipped"}
                self.progress.checkpoint()
                continue

            result = step_func()
            results[step_name] = result
            self.progress.checkpoint()

            if result.get("status") == "error":
                if step_name not in self.progress.step_status:
                    self.progress.update_step_status(step_name, "error", "步骤执行失败")
                self.progress.add_log(step_name, "error", f"步骤失败，停止执行")
                break

        return {
            "results": results,
            "progress": self.progress.get_progress(),
            "logs": self.progress.progress_log
        }


def create_staged_executor(
    config: Dict[str, Any],
    *,
    logger_instance=logger,
    log_callback: Optional[Callable[[str, str, str, str], None]] = None,
    status_callback: Optional[Callable[[str, str, int, str], None]] = None,
    cancel_callback: Optional[Callable[[], None]] = None,
    executors: Optional[Dict[str, Any]] = None,
) -> Tuple[StagedSetupExecutor, StagedSetupProgress]:
    """
    创建阶段化执行器实例

    Args:
        config: 扁平化配置字典
        logger_instance: 日志记录器
        log_callback: 日志回调
        status_callback: 状态回调
        cancel_callback: 取消回调
        executors: 需要注入的 SSH/SQL 执行器

    Returns:
        (executor, progress_tracker)
    """
    progress = StagedSetupProgress(
        stage_sequence=StagedSetupExecutor.STAGE_SEQUENCE,
        log_callback=log_callback,
        status_callback=status_callback,
        cancel_callback=cancel_callback,
    )
    progress.start()
    executor = StagedSetupExecutor(config, progress, logger_instance=logger_instance)
    if executors:
        for attr in ("primary_ssh", "standby_ssh", "primary_sql", "standby_sql"):
            if executors.get(attr):
                setattr(executor, attr, executors[attr])
    return executor, progress
