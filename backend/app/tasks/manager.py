"""任务管理器 - 管理后台任务"""

import json
import uuid
import time
import atexit
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from datetime import datetime
from threading import Thread, Lock, Event

from pydantic import ValidationError
from pydantic.json import pydantic_encoder

from loguru import logger

from ..core.config import config_manager
from ..models.schemas import TaskStatus


class TaskCancelledException(Exception):
    """Raised when a running task observes a cancellation request."""

    def __init__(self, task_id: str, message: str = "任务被取消"):
        super().__init__(message)
        self.task_id = task_id


class TaskManager:
    """
    任务管理器

    使用简单线程池实现后台任务，生产环境应替换为 Celery。
    """

    def __init__(
        self,
        max_concurrent_tasks: int = 5,
        task_store_path: Optional[str] = None,
        persist_flush_interval: float = 0.3
    ):
        """
        初始化任务管理器

        Args:
            max_concurrent_tasks: 最大并发任务数
        """
        self.max_concurrent_tasks = max_concurrent_tasks
        self.tasks: Dict[str, TaskStatus] = {}
        self.lock = Lock()
        self._running_count = 0
        self.task_store_path = Path(task_store_path or "data/tasks.json")
        self.task_store_path.parent.mkdir(parents=True, exist_ok=True)
        self._persist_flush_interval = max(persist_flush_interval, 0.05)
        self._save_event = Event()
        self._stop_event = Event()
        self._persist_thread: Optional[Thread] = None
        self._atexit_registered = False

        self._load_tasks_from_file()
        self._start_persist_thread()

    def create_task(
        self,
        action: str,
        cluster_id: str,
        execute_func: Callable,
        **kwargs
    ) -> str:
        """
        创建并启动一个新任务

        Args:
            action: 操作类型
            cluster_id: 集群 ID
            execute_func: 执行函数，接受一个 logger 参数
            **kwargs: 执行函数的额外参数

        Returns:
            任务 ID
        """
        task_id = str(uuid.uuid4())

        task = TaskStatus(
            task_id=task_id,
            action=action,
            cluster_id=cluster_id,
            status="pending",
            progress=0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        with self.lock:
            self.tasks[task_id] = task

        self._schedule_persist(immediate=True)

        # 在新线程中执行任务
        thread = Thread(
            target=self._execute_task,
            args=(task_id, execute_func, kwargs),
            daemon=True
        )
        thread.start()

        logger.info(f"任务创建成功: {task_id}, 操作: {action}, 集群: {cluster_id}")

        return task_id

    def _execute_task(
        self,
        task_id: str,
        execute_func: Callable,
        execute_kwargs: Dict[str, Any]
    ):
        """
        执行任务（在独立线程中）

        Args:
            task_id: 任务 ID
            execute_func: 执行函数
            execute_kwargs: 执行参数
        """
        # 等待可用槽位
        while True:
            if self._is_cancelled(task_id):
                logger.info(f"任务在排队阶段被取消: {task_id}")
                return
            with self.lock:
                if self._running_count < self.max_concurrent_tasks:
                    self._running_count += 1
                    break
            time.sleep(0.5)

        try:
            self._raise_if_cancelled(task_id)

            # 更新任务状态为运行中
            self._update_task_status(task_id, "running", progress=0)

            # 创建任务专用的 logger
            task_logger = self._create_task_logger(task_id)

            self._raise_if_cancelled(task_id)

            # 执行任务函数
            result = execute_func(logger=task_logger, **execute_kwargs)

            self._raise_if_cancelled(task_id)

            # 更新任务状态为成功
            self._update_task_status(
                task_id,
                "success",
                progress=100,
                result=result
            )

        except TaskCancelledException as cancel_exc:
            logger.info(f"任务执行被取消: {task_id}, 原因: {cancel_exc}")
            self._add_task_log(task_id, "检测到取消信号，任务执行已停止")
            self._update_task_status(task_id, "cancelled")

        except Exception as e:
            logger.error(f"任务执行失败: {task_id}, 错误: {e}")

            # 更新任务状态为失败
            self._update_task_status(
                task_id,
                "failed",
                error=str(e)
            )

        finally:
            with self.lock:
                self._running_count -= 1

    def _create_task_logger(self, task_id: str):
        """创建任务专用的 logger"""

        class TaskLogger:
            def __init__(self, manager: TaskManager, task_id: str):
                self.manager = manager
                self.task_id = task_id

            def is_cancelled(self) -> bool:
                """Return True if current task has been cancelled."""
                return self.manager._is_cancelled(self.task_id)

            def raise_if_cancelled(self):
                """Raise TaskCancelledException when task is cancelled."""
                self.manager._raise_if_cancelled(self.task_id)

            def info(self, message: str):
                logger.info(f"[{self.task_id}] {message}")
                self.manager._add_task_log(self.task_id, message)

            def warning(self, message: str):
                logger.warning(f"[{self.task_id}] {message}")
                self.manager._add_task_log(self.task_id, f"WARNING: {message}")

            def error(self, message: str):
                logger.error(f"[{self.task_id}] {message}")
                self.manager._add_task_log(self.task_id, f"ERROR: {message}")

            def debug(self, message: str):
                logger.debug(f"[{self.task_id}] {message}")
                self.manager._add_task_log(self.task_id, f"DEBUG: {message}")

            def set_progress(self, value: int, message: Optional[str] = None):
                """更新任务进度并可选记录提示"""
                self.raise_if_cancelled()
                clamped = max(0, min(100, value))
                self.manager._update_task_status(
                    self.task_id,
                    "running",
                    progress=clamped
                )
                if message:
                    self.manager._add_task_log(self.task_id, f"PROGRESS: {message}")

        return TaskLogger(self, task_id)

    def _update_task_status(
        self,
        task_id: str,
        status: str,
        progress: Optional[int] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None
    ):
        """更新任务状态"""
        should_persist = False
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return

            if task.status == "cancelled" and status != "cancelled":
                return

            task.status = status
            task.updated_at = datetime.utcnow()
            should_persist = True

            if progress is not None:
                task.progress = progress

            if status in ("success", "failed", "cancelled"):
                task.completed_at = datetime.utcnow()

            if result is not None:
                task.result = result

            if error:
                task.result = task.result or {}
                task.result["error"] = error

        if should_persist:
            self._schedule_persist()

    def _add_task_log(self, task_id: str, log_message: str):
        """添加任务日志"""
        should_persist = False
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                timestamp = datetime.utcnow().isoformat()
                task.logs.append(f"{timestamp}: {log_message}")
                task.updated_at = datetime.utcnow()
                should_persist = True

                # 限制日志条数
                if len(task.logs) > 1000:
                    task.logs = task.logs[-1000:]

        if should_persist:
            self._schedule_persist()

    def _start_persist_thread(self):
        """启动持久化后台线程"""
        if self._persist_thread and self._persist_thread.is_alive():
            return

        self._persist_thread = Thread(
            target=self._persist_loop,
            name="task-persist-writer",
            daemon=True
        )
        self._persist_thread.start()

        if not self._atexit_registered:
            atexit.register(self._shutdown_persistor)
            self._atexit_registered = True

    def _persist_loop(self):
        """后台循环，处理批量持久化"""
        while not self._stop_event.is_set():
            triggered = self._save_event.wait(timeout=1.0)
            if not triggered:
                continue

            if self._stop_event.is_set():
                break

            time.sleep(self._persist_flush_interval)
            self._save_event.clear()
            try:
                self._save_tasks_to_file()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"持久化任务失败: {exc}")

        try:
            self._save_tasks_to_file()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"退出时持久化任务失败: {exc}")

    def _schedule_persist(self, immediate: bool = False):
        """调度持久化写入"""
        if immediate:
            try:
                self._save_tasks_to_file()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"同步持久化任务失败: {exc}")
            return

        self._save_event.set()

    def _load_tasks_from_file(self):
        """加载历史任务"""
        if not self.task_store_path.exists():
            return

        try:
            with open(self.task_store_path, "r", encoding="utf-8") as f:
                raw_tasks = json.load(f)
        except FileNotFoundError:
            return
        except json.JSONDecodeError as exc:
            logger.error(f"解析任务文件失败: {exc}")
            return
        except OSError as exc:
            logger.error(f"读取任务文件失败: {exc}")
            return

        if not isinstance(raw_tasks, list):
            logger.warning("任务文件格式无效，已忽略")
            return

        loaded: Dict[str, TaskStatus] = {}
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            try:
                task = TaskStatus(**item)
            except ValidationError as exc:
                logger.warning(f"忽略无效任务记录: {exc}")
                continue
            loaded[task.task_id] = task

        if not loaded:
            return

        with self.lock:
            self.tasks.update(loaded)

        logger.info(f"加载历史任务 {len(loaded)} 条")

    def _save_tasks_to_file(self):
        """保存任务状态到文件"""
        with self.lock:
            tasks_snapshot = [
                task.dict()
                for task in sorted(
                    self.tasks.values(),
                    key=lambda t: t.created_at
                )
            ]
            tmp_path = self.task_store_path.parent / f"{self.task_store_path.name}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(
                        tasks_snapshot,
                        f,
                        ensure_ascii=False,
                        indent=2,
                        default=pydantic_encoder
                    )
                tmp_path.replace(self.task_store_path)
            except OSError as exc:
                logger.error(f"保存任务文件失败: {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"写入任务文件时出现未知错误: {exc}")

    def _shutdown_persistor(self):
        """应用退出时确保持久化线程关闭"""
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
        if hasattr(self, "_save_event"):
            self._save_event.set()

        thread = getattr(self, "_persist_thread", None)
        if thread and thread.is_alive():
            thread.join(timeout=2)

        try:
            self._save_tasks_to_file()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"关闭持久化线程时保存失败: {exc}")

    def _is_cancelled(self, task_id: str) -> bool:
        """检查任务是否已取消"""
        with self.lock:
            task = self.tasks.get(task_id)
            return bool(task and task.status == "cancelled")

    def _raise_if_cancelled(self, task_id: str):
        """若任务已取消则抛出异常以中断执行"""
        if self._is_cancelled(task_id):
            raise TaskCancelledException(task_id)

    def cancel_task(self, task_id: str) -> Optional[TaskStatus]:
        """取消任务"""
        cancelled_task: Optional[TaskStatus] = None
        should_persist = False
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None

            if task.status in ("success", "failed", "cancelled"):
                return task

            task.status = "cancelled"
            task.updated_at = datetime.utcnow()
            task.completed_at = datetime.utcnow()
            task.result = task.result or {}
            task.result["cancelled"] = True
            task.result["message"] = "任务被用户取消"
            cancelled_task = task
            should_persist = True

        if should_persist:
            self._schedule_persist()

        self._add_task_log(task_id, "任务被用户取消")
        logger.info(f"任务被取消: {task_id}")
        return cancelled_task

    def get_task(self, task_id: str) -> Optional[TaskStatus]:
        """获取任务状态"""
        with self.lock:
            return self.tasks.get(task_id)

    def list_tasks(
        self,
        cluster_id: Optional[str] = None,
        status: Optional[str] = None
    ) -> list:
        """获取任务列表"""
        with self.lock:
            tasks = list(self.tasks.values())

            if cluster_id:
                tasks = [t for t in tasks if t.cluster_id == cluster_id]

            if status:
                tasks = [t for t in tasks if t.status == status]

            # 按创建时间倒序
            tasks.sort(key=lambda t: t.created_at, reverse=True)

            return tasks


# 全局任务管理器实例
task_manager = TaskManager(
    max_concurrent_tasks=config_manager.app_config.max_concurrent_tasks,
    task_store_path=config_manager.app_config.task_store_path,
)
