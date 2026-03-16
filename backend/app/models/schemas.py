"""Pydantic 数据模型"""

import json
from typing import Optional, Dict, Any, List
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field, validator
from ..core.config import OracleConfig, SSHConfig


class ClusterInfo(BaseModel):
    """集群基本信息"""
    cluster_id: str
    cluster_name: str
    db_type: str
    created_at: datetime
    description: Optional[str] = None


class PrimaryStatus(BaseModel):
    """主库状态"""
    role: str = "PRIMARY"
    host: str
    connected: bool
    database_role: Optional[str] = None
    open_mode: Optional[str] = None
    protection_mode: Optional[str] = None
    last_archived_sequence: Optional[str] = None


class StandbyStatus(BaseModel):
    """备库状态"""
    role: str = "STANDBY"
    host: str
    connected: bool
    database_role: Optional[str] = None
    open_mode: Optional[str] = None
    protection_mode: Optional[str] = None
    mrp_status: Optional[str] = None
    applied_sequence: Optional[str] = None
    last_applied_time: Optional[str] = None


class SyncStatus(BaseModel):
    """同步状态"""
    status: str = "UNKNOWN"  # SYNCED, LAGGING, ERROR
    lag_seconds: float = 0
    lag_sequence: int = 0
    gap: bool = False


class ClusterStatus(BaseModel):
    """集群完整状态"""
    cluster_id: str
    cluster_name: str
    timestamp: datetime
    primary: PrimaryStatus
    standby: StandbyStatus
    sync: SyncStatus
    error: Optional[str] = None


class SyncHistoryItem(BaseModel):
    """同步延迟历史数据点"""
    timestamp: datetime
    lag_seconds: float
    status: str


class TablespaceUsage(BaseModel):
    """表空间使用情况"""
    tablespace_name: str
    mb_used: float
    mb_max: float
    usage_percent: float


class ArchiveLogRetention(BaseModel):
    """归档日志保留信息"""
    name: str
    size_mb: float


class ResourceTotals(BaseModel):
    """资源汇总信息"""
    tablespace_used_mb: float = 0.0
    tablespace_total_mb: float = 0.0
    archive_total_mb: float = 0.0


class ResourceStatus(BaseModel):
    """资源监控状态"""
    cluster_id: str
    collected_at: datetime
    tablespaces: List[TablespaceUsage] = Field(default_factory=list)
    archive_logs: List[ArchiveLogRetention] = Field(default_factory=list)
    totals: ResourceTotals = Field(default_factory=ResourceTotals)


class TaskStatus(BaseModel):
    """任务状态"""
    task_id: str
    action: str
    cluster_id: str
    status: str  # pending, running, success, failed, cancelled
    progress: int = 0
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None
    logs: List[str] = Field(default_factory=list)


class SwitchoverRequest(BaseModel):
    """切换请求"""
    dry_run: bool = False
    comment: Optional[str] = None


class FailoverRequest(BaseModel):
    """接管请求"""
    dry_run: bool = False
    comment: Optional[str] = None
    confirm: bool = False  # 二次确认标识


class ManageRequest(BaseModel):
    """管理操作请求"""
    action: str  # sync, cleanup_archive, backup, recovery
    parameters: Dict[str, Any] = Field(default_factory=dict)


class ClusterConfigPayload(BaseModel):
    """集群配置载荷"""
    cluster_name: str = Field(..., min_length=1, max_length=128)
    db_type: str = Field(default="oracle", description="数据库类型")
    primary: OracleConfig
    primary_ssh: SSHConfig
    standby: OracleConfig
    standby_ssh: SSHConfig
    data_files_path: str = Field(..., min_length=1)
    archivelog_path: str = Field(..., min_length=1)


class CreateClusterRequest(ClusterConfigPayload):
    """创建集群请求"""
    cluster_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{3,64}$")


class UpdateClusterRequest(ClusterConfigPayload):
    """更新集群请求"""
    pass


# ============ 阶段化搭建相关模型 ============

class SetupStepStatus(str, Enum):
    """搭建步骤状态枚举"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING_APPROVAL = "waiting_approval"


class SetupStageStatus(str, Enum):
    """搭建阶段状态枚举"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class RiskLevel(str, Enum):
    """风险级别枚举"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SetupStepDetail(BaseModel):
    """单步执行记录"""
    step_id: str
    step_name: str
    display_name: str
    description: Optional[str] = None
    target_host: Optional[str] = None
    status: SetupStepStatus = SetupStepStatus.PENDING
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    exit_code: Optional[int] = None
    risk_level: RiskLevel = RiskLevel.LOW
    is_idempotent: bool = True
    retry_count: int = 0
    error_message: Optional[str] = None
    checkpoint_data: Optional[Dict[str, Any]] = None  # 用于幂等检查


class SetupStageDetail(BaseModel):
    """阶段执行记录"""
    stage_name: str
    display_name: str
    description: Optional[str] = None
    status: SetupStageStatus = SetupStageStatus.PENDING
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    steps: List[SetupStepDetail] = Field(default_factory=list)
    error_message: Optional[str] = None
    summary: Optional[Dict[str, Any]] = None


class SetupTaskProgress(BaseModel):
    """搭建任务进度"""
    current_stage: Optional[str] = None
    total_stages: int = 0
    completed_stages: int = 0
    stages: Dict[str, SetupStageDetail] = Field(default_factory=dict)
    progress_percent: int = 0
    elapsed_seconds: float = 0
    overall_status: SetupStageStatus = SetupStageStatus.PENDING


# 扩展 TaskStatus 的 result 字段类型
SetupTaskResult = Dict[str, Any]


# ============ Oracle ADG 预检查与预览模型 ============


class OracleEnvInfo(BaseModel):
    """Oracle 环境信息"""

    version: Optional[str] = None
    oracle_home: Optional[str] = None
    oracle_sid: Optional[str] = None
    oracle_base: Optional[str] = None
    storage_type: Optional[str] = None
    listener_port: Optional[int] = Field(default=None, ge=1, le=65535)
    is_cdb: Optional[bool] = None
    db_unique_name: Optional[str] = None
    service_name: Optional[str] = None


class DiscoveryInfo(BaseModel):
    """自动探测结果"""

    primary_host: Dict[str, Any] = Field(default_factory=dict)
    standby_host: Dict[str, Any] = Field(default_factory=dict)
    primary_oracle: OracleEnvInfo = Field(default_factory=OracleEnvInfo)
    standby_oracle: OracleEnvInfo = Field(default_factory=OracleEnvInfo)
    primary_db: Dict[str, Any] = Field(default_factory=dict)
    network: Dict[str, Any] = Field(default_factory=dict)
    storage: Dict[str, Any] = Field(default_factory=dict)
    conflicts: List[str] = Field(default_factory=list)


class PrecheckResult(BaseModel):
    """单项检查结果"""

    check_name: str
    category: str  # compatibility, connectivity, storage, configuration, security
    result: str  # pass, warn, fail
    message: str
    evidence: Optional[Dict[str, Any]] = None
    suggestion: Optional[str] = None
    blocking: bool = False
    risk_level: RiskLevel = RiskLevel.LOW


class RiskSummary(BaseModel):
    """风险汇总"""

    total_checks: int = 0
    passed: int = 0
    warned: int = 0
    failed: int = 0
    blocking_issues: List[str] = Field(default_factory=list)
    high_risk_steps: List[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    """单步执行计划"""

    step_id: str
    title: str
    description: str
    target_host: str  # primary, standby, management
    executor_type: str  # ssh, sqlplus, rman
    command_preview: str  # 脱敏摘要，不包含密码等敏感信息
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    rollback_capability: bool = True
    estimated_duration: Optional[int] = None  # seconds


class PlanStage(BaseModel):
    """执行计划中的阶段"""

    stage_name: str
    display_name: str
    description: Optional[str] = None
    steps: List[PlanStep] = Field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None

    @validator("metadata")
    def _metadata_must_be_jsonable(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """确保 metadata 字段可序列化为 JSON，便于前端消费"""
        if value is None:
            return None
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            raise ValueError(f"metadata must be JSON serializable: {exc}") from exc
        return value


class ExecutionPlan(BaseModel):
    """执行计划"""

    stages: List[PlanStage] = Field(default_factory=list)
    total_steps: int = 0
    estimated_total_duration: Optional[int] = None  # seconds
    approval_required: bool = False


class PreviewRequest(BaseModel):
    """预览请求"""

    # SSH 配置
    primary_host: str = Field(..., min_length=1)
    primary_ssh_port: int = Field(default=22, ge=1, le=65535)
    primary_ssh_user: str = Field(..., min_length=1)
    primary_ssh_auth_type: str = Field(default="key", description="key 或 password")
    primary_ssh_key_path: Optional[str] = None
    primary_ssh_password: Optional[str] = None

    standby_host: str = Field(..., min_length=1)
    standby_ssh_port: int = Field(default=22, ge=1, le=65535)
    standby_ssh_user: str = Field(..., min_length=1)
    standby_ssh_auth_type: str = Field(default="key")
    standby_ssh_key_path: Optional[str] = None
    standby_ssh_password: Optional[str] = None

    # Oracle 配置
    oracle_sid: str = Field(..., min_length=1)
    oracle_home: str = Field(default="/u01/app/oracle/product/19c/dbhome_1")
    sys_password: Optional[str] = None

    # ADG 配置
    db_name: str = Field(default="DG", min_length=1)
    db_unique_name_primary: str = Field(default="DG_PRIMARY")
    db_unique_name_standby: str = Field(default="DG_STANDBY")
    storage_type: str = Field(default="fs", description="asm 或 fs")
    duplicate_mode: str = Field(default="backup", description="active 或 backup")
    protection_mode: str = Field(default="MAXIMUM PERFORMANCE")
    enable_realtime_apply: bool = True

    # 路径配置
    data_files_path: str = Field(default="/u01/oradata/ORCL")
    archivelog_path: str = Field(default="/u01/oradata/ORCL/archivelog")
    backup_path: Optional[str] = None

    # 新增：主库 Oracle 配置
    primary_sid: Optional[str] = None
    primary_oracle_home: Optional[str] = None
    primary_oracle_base: Optional[str] = None
    primary_listener_port: Optional[int] = Field(default=1521, ge=1, le=65535)
    primary_service_name: Optional[str] = None
    primary_storage_type: Optional[str] = None
    primary_is_cdb: Optional[bool] = None

    # 新增：备库 Oracle 配置
    standby_sid: Optional[str] = None
    standby_oracle_home: Optional[str] = None
    standby_oracle_base: Optional[str] = None
    standby_listener_port: Optional[int] = Field(default=1521, ge=1, le=65535)
    standby_service_name: Optional[str] = None
    standby_storage_type: Optional[str] = None
    standby_is_cdb: Optional[bool] = None

    # 新增：日志传输与 SRL
    log_transport_mode: Optional[str] = Field(default="ASYNC", description="ASYNC 或 SYNC")
    auto_create_srl: Optional[bool] = None

    # 新增：路径策略
    data_file_path_strategy: Optional[str] = None
    redo_file_path_strategy: Optional[str] = None
    primary_data_file_path: Optional[str] = None
    standby_data_file_path: Optional[str] = None
    primary_redo_file_path: Optional[str] = None
    standby_redo_file_path: Optional[str] = None

    # 新增：归档清理策略
    archive_cleanup_policy: Optional[str] = None
    archive_cleanup_param: Optional[int] = None


class PreviewResponse(BaseModel):
    """预览响应"""

    discovered_info: DiscoveryInfo = Field(default_factory=DiscoveryInfo)
    precheck_results: List[PrecheckResult] = Field(default_factory=list)
    execution_plan: ExecutionPlan = Field(default_factory=ExecutionPlan)
    missing_inputs: List[str] = Field(default_factory=list)
    risk_summary: RiskSummary = Field(default_factory=RiskSummary)
    preview_generated_at: datetime = Field(default_factory=datetime.utcnow)
    is_demo: bool = False
    error_type: Optional[str] = None
