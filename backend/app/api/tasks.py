"""任务管理 API"""

from fastapi import APIRouter, HTTPException
from typing import List, Optional

from ..models.schemas import TaskStatus
from ..models.response import APIResponse
from ..tasks.manager import task_manager

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=APIResponse[TaskStatus])
async def get_task_status(task_id: str):
    """
    获取任务状态和进度

    Args:
        task_id: 任务 ID

    Returns:
        任务状态信息
    """
    task = task_manager.get_task(task_id)

    if not task:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    return APIResponse(data=task)


@router.get("/", response_model=APIResponse[List[TaskStatus]])
async def list_tasks(cluster_id: Optional[str] = None, status: Optional[str] = None):
    """
    获取任务列表

    Args:
        cluster_id: 可选，按集群 ID 筛选
        status: 可选，按状态筛选

    Returns:
        任务列表
    """
    tasks = task_manager.list_tasks(cluster_id=cluster_id, status=status)

    return APIResponse(data=tasks)


@router.get("/{task_id}/logs", response_model=APIResponse[List[str]])
async def get_task_logs(task_id: str):
    """获取任务日志"""
    task = task_manager.get_task(task_id)

    if not task:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    return APIResponse(data=task.logs)


@router.delete("/{task_id}", response_model=APIResponse[TaskStatus])
async def cancel_task(task_id: str):
    """取消任务"""
    task = task_manager.cancel_task(task_id)

    if not task:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    return APIResponse(data=task)
