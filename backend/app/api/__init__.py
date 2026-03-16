"""API 路由模块"""

from .clusters import router as clusters_router
from .tasks import router as tasks_router
from .health import router as health_router
from .oracle_adg import router as oracle_adg_router

__all__ = ["clusters_router", "tasks_router", "health_router", "oracle_adg_router"]
