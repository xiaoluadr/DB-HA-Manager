"""健康检查 API"""

from fastapi import APIRouter
from datetime import datetime

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/")
async def health_check():
    """健康检查接口"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "DB-HA-Manager",
        "version": "0.1.0"
    }
