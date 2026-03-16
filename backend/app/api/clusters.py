"""集群管理 API"""

from typing import Dict, List, Any, Optional
from datetime import datetime
from fastapi import APIRouter, HTTPException, status
from pydantic import ValidationError

from ..models.schemas import (
    ClusterInfo,
    ClusterStatus,
    SyncHistoryItem,
    ResourceStatus,
    SwitchoverRequest,
    FailoverRequest,
    ManageRequest,
    CreateClusterRequest,
    UpdateClusterRequest,
)
from ..models.response import APIResponse
from ..core.config import config_manager, ClusterConfig
from ..drivers import DatabaseHADriver, OracleDataGuardDriver
from ..tasks.manager import task_manager

router = APIRouter(prefix="/api/clusters", tags=["clusters"])


# 驱动实例缓存
_driver_cache: Dict[str, DatabaseHADriver] = {}
_SUPPORTED_DB_TYPES = {"oracle"}


def _normalize_db_type(db_type: str) -> str:
    """标准化数据库类型字符串"""
    return (db_type or "").strip().lower()


def _ensure_supported_db(db_type: str) -> str:
    """校验数据库类型是否受支持并返回标准值"""
    normalized = _normalize_db_type(db_type)
    if normalized not in _SUPPORTED_DB_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的数据库类型: {db_type}")
    return normalized


def _cluster_to_info(cluster_id: str, cluster_config: ClusterConfig) -> ClusterInfo:
    """将 ClusterConfig 转换为 ClusterInfo"""
    return ClusterInfo(
        cluster_id=cluster_id,
        cluster_name=cluster_config.cluster_name,
        db_type=cluster_config.db_type,
        created_at=datetime.utcnow(),
    )


def _invalidate_driver_cache(cluster_id: str):
    """删除指定集群的驱动缓存"""
    _driver_cache.pop(cluster_id, None)


def _get_driver(cluster_id: str) -> DatabaseHADriver:
    """获取或创建驱动实例"""
    if cluster_id in _driver_cache:
        return _driver_cache[cluster_id]

    cluster_config = config_manager.get_cluster(cluster_id)
    if not cluster_config:
        raise HTTPException(status_code=404, detail=f"集群 {cluster_id} 不存在")

    # 根据数据库类型创建对应的驱动
    if cluster_config.db_type == "oracle":
        driver = OracleDataGuardDriver(cluster_id, cluster_config)
    else:
        raise HTTPException(status_code=400, detail=f"不支持的数据库类型: {cluster_config.db_type}")

    _driver_cache[cluster_id] = driver
    return driver


@router.get("/", response_model=APIResponse[List[ClusterInfo]])
async def list_clusters():
    """
    获取所有集群列表

    返回:
        集群基本信息列表
    """
    clusters = config_manager.list_clusters()

    cluster_list = [
        _cluster_to_info(cluster_id, cluster)
        for cluster_id, cluster in clusters.items()
    ]

    return APIResponse(data=cluster_list)


@router.post("/", response_model=APIResponse[ClusterInfo], status_code=status.HTTP_201_CREATED)
async def create_cluster(request: CreateClusterRequest):
    """
    创建新的数据库集群配置
    """
    if config_manager.get_cluster(request.cluster_id):
        raise HTTPException(status_code=409, detail=f"集群 {request.cluster_id} 已存在")

    normalized_type = _ensure_supported_db(request.db_type)
    payload = request.dict()
    payload["db_type"] = normalized_type

    try:
        cluster_config = ClusterConfig(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"配置校验失败: {exc}") from exc

    config_manager.save_cluster(cluster_config)
    _invalidate_driver_cache(cluster_config.cluster_id)

    cluster_info = _cluster_to_info(cluster_config.cluster_id, cluster_config)
    return APIResponse(data=cluster_info, message="集群创建成功")


@router.put("/{cluster_id}", response_model=APIResponse[ClusterInfo])
async def update_cluster(cluster_id: str, request: UpdateClusterRequest):
    """
    更新指定集群的配置
    """
    existing = config_manager.get_cluster(cluster_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"集群 {cluster_id} 不存在")

    normalized_type = _ensure_supported_db(request.db_type)
    payload = request.dict()
    payload["cluster_id"] = cluster_id
    payload["db_type"] = normalized_type

    try:
        cluster_config = ClusterConfig(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"配置校验失败: {exc}") from exc

    config_manager.save_cluster(cluster_config)
    _invalidate_driver_cache(cluster_id)

    cluster_info = _cluster_to_info(cluster_id, cluster_config)
    return APIResponse(data=cluster_info, message="集群更新成功")


@router.delete("/{cluster_id}", response_model=APIResponse[Dict[str, Any]])
async def delete_cluster(cluster_id: str):
    """
    删除指定集群配置
    """
    existing = config_manager.get_cluster(cluster_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"集群 {cluster_id} 不存在")

    config_manager.delete_cluster(cluster_id)
    _invalidate_driver_cache(cluster_id)

    return APIResponse(data={"cluster_id": cluster_id, "deleted": True}, message="集群已删除")


@router.get("/{cluster_id}/status", response_model=APIResponse[ClusterStatus])
async def get_cluster_status(cluster_id: str):
    """
    获取指定集群的主备状态

    Args:
        cluster_id: 集群 ID

    Returns:
        集群状态信息
    """
    driver = _get_driver(cluster_id)
    status = driver.status()

    return APIResponse(data=status)


@router.get("/{cluster_id}/sync-history", response_model=APIResponse[List[SyncHistoryItem]])
async def get_cluster_sync_history(
    cluster_id: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None
):
    """
    获取指定集群的同步延迟历史数据
    """
    if start_time and end_time and start_time > end_time:
        raise HTTPException(status_code=400, detail="start_time 不能大于 end_time")

    driver = _get_driver(cluster_id)
    if not isinstance(driver, OracleDataGuardDriver):
        raise HTTPException(status_code=400, detail="仅支持 Oracle 数据库的同步历史查询")

    try:
        history = driver.get_sync_history(start_time=start_time, end_time=end_time)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取同步历史失败: {exc}") from exc

    return APIResponse(data=history)


@router.get("/{cluster_id}/resources", response_model=APIResponse[ResourceStatus])
async def get_cluster_resource_status(cluster_id: str):
    """
    获取指定集群的资源使用情况（表空间、归档日志）
    """
    driver = _get_driver(cluster_id)

    if not isinstance(driver, OracleDataGuardDriver):
        raise HTTPException(status_code=400, detail="仅支持 Oracle 数据库的资源查询")

    try:
        resources = driver.resource_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取资源信息失败: {exc}") from exc

    return APIResponse(data=resources)


@router.get("/{cluster_id}/setup/commands", response_model=APIResponse[Dict[str, List[Dict[str, str]]]])
async def get_setup_commands(cluster_id: str):
    """
    获取搭建流程的所有命令供前端审批

    Args:
        cluster_id: 集群 ID

    Returns:
        所有步骤的命令列表
    """
    driver = _get_driver(cluster_id)

    # 确保是 Oracle 驱动
    if not isinstance(driver, OracleDataGuardDriver):
        raise HTTPException(status_code=400, detail="仅支持 Oracle 数据库的搭建命令预览")

    commands = driver.generate_setup_commands()

    return APIResponse(data=commands)


@router.post("/{cluster_id}/setup/approve", response_model=APIResponse[Dict[str, Any]])
async def approve_setup(cluster_id: str, approved_steps: Dict[str, List[str]]):
    """
    审批并执行搭建流程

    Args:
        cluster_id: 集群 ID
        approved_steps: 用户审批的步骤列表 {"steps": ["step1", "step2", ...]}

    Returns:
        任务 ID 和被跳过的步骤
    """
    driver = _get_driver(cluster_id)

    # 仅支持 Oracle 搭建审批
    if not isinstance(driver, OracleDataGuardDriver):
        raise HTTPException(status_code=400, detail="仅支持 Oracle 数据库的搭建审批")

    steps_payload = approved_steps.get("steps")
    if steps_payload is None or not isinstance(steps_payload, list):
        raise HTTPException(status_code=400, detail="approved_steps 参数格式错误，需包含 steps 列表")

    normalized_steps: List[str] = []
    seen = set()
    for step in steps_payload:
        if not isinstance(step, str):
            raise HTTPException(status_code=400, detail="步骤名称必须为字符串")
        name = step.strip()
        if not name:
            continue
        if name not in seen:
            seen.add(name)
            normalized_steps.append(name)

    try:
        commands = driver.generate_setup_commands()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"无法获取搭建步骤: {exc}") from exc

    all_steps = list(commands.keys())
    if not all_steps:
        raise HTTPException(status_code=500, detail="未找到可执行的搭建步骤")

    unknown_steps = [step for step in normalized_steps if step not in all_steps]
    if unknown_steps:
        raise HTTPException(
            status_code=400,
            detail=f"存在未知步骤: {', '.join(unknown_steps)}"
        )

    approved_set = set(normalized_steps)
    skipped_steps = [step for step in all_steps if step not in approved_set]

    def _execute_setup_task(
        logger,
        driver: OracleDataGuardDriver,
        skip_steps: List[str],
        approved: List[str]
    ):
        approved_msg = ", ".join(approved) if approved else "无"
        skip_msg = ", ".join(skip_steps) if skip_steps else "无"
        logger.info(f"审批通过步骤: {approved_msg}")
        logger.info(f"跳过步骤: {skip_msg}")

        cancel_callback = getattr(logger, "raise_if_cancelled", None)

        def _log_callback(step: str, level: str, message: str, _result: str):
            log_text = f"[{step}] {message}"
            log_method = getattr(logger, level, logger.info)
            log_method(log_text)

        def _status_callback(step: str, status_text: str, percent: int, message: str):
            progress_message = message or f"{step} -> {status_text}"
            if hasattr(logger, "set_progress"):
                logger.set_progress(percent, progress_message)

        if cancel_callback:
            cancel_callback()

        result = driver.execute_all_steps(
            skip_steps=skip_steps,
            log_callback=_log_callback,
            status_callback=_status_callback,
            cancel_callback=cancel_callback
        )
        result["approved_steps"] = approved
        result["skipped_steps"] = skip_steps
        return result

    task_id = task_manager.create_task(
        action="setup_approve",
        cluster_id=cluster_id,
        execute_func=_execute_setup_task,
        driver=driver,
        skip_steps=skipped_steps,
        approved=normalized_steps,
    )

    return APIResponse(data={"task_id": task_id, "skipped_steps": skipped_steps})


@router.post(
    "/{cluster_id}/setup",
    response_model=APIResponse[Dict[str, str]],
    status_code=status.HTTP_202_ACCEPTED
)
async def setup_cluster(cluster_id: str, config: Dict[str, Any]):
    """
    发起集群搭建

    Args:
        cluster_id: 集群 ID
        config: 搭建配置

    Returns:
        任务 ID
    """
    driver = _get_driver(cluster_id)
    execute_func = driver.setup(config)

    if not callable(execute_func):
        raise HTTPException(status_code=500, detail="驱动未返回有效的任务执行器")

    task_id = task_manager.create_task(
        action="setup",
        cluster_id=cluster_id,
        execute_func=execute_func
    )

    return APIResponse(data={"task_id": task_id})


@router.post("/{cluster_id}/switchover", response_model=APIResponse[Dict[str, Any]])
async def execute_switchover(cluster_id: str, request: SwitchoverRequest):
    """
    执行 Switchover（正常切换）

    Args:
        cluster_id: 集群 ID
        request: 切换请求参数

    Returns:
        切换结果
    """
    driver = _get_driver(cluster_id)
    result = driver.switchover(dry_run=request.dry_run)

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error"))

    return APIResponse(data=result)


@router.post("/{cluster_id}/failover", response_model=APIResponse[Dict[str, Any]])
async def execute_failover(cluster_id: str, request: FailoverRequest):
    """
    执行 Failover（应急接管）

    Args:
        cluster_id: 集群 ID
        request: 接管请求参数

    Returns:
        接管结果
    """
    if not request.confirm:
        raise HTTPException(
            status_code=400,
            detail="需要二次确认才能执行 Failover，请设置 confirm=true"
        )

    driver = _get_driver(cluster_id)
    result = driver.failover(dry_run=request.dry_run)

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error"))

    return APIResponse(data=result)


@router.post("/{cluster_id}/manage", response_model=APIResponse[Dict[str, Any]])
async def manage_cluster(cluster_id: str, request: ManageRequest):
    """
    执行管理操作

    Args:
        cluster_id: 集群 ID
        request: 管理操作请求

    Returns:
        操作结果
    """
    driver = _get_driver(cluster_id)
    result = driver.manage(request.action, **request.parameters)

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error"))

    return APIResponse(data=result)
