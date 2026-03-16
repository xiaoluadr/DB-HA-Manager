"""DB-HA-Manager 后端主入口"""

import os
import re
from contextlib import asynccontextmanager
from typing import List, Optional, Sequence, Tuple

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import health_router, clusters_router, tasks_router, oracle_adg_router
from .core.config import DEFAULT_ALLOWED_ORIGINS, config_manager
from .core.logger import logger


ALLOWED_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
ALLOWED_HEADERS = ["Content-Type", "Authorization", "X-Requested-With"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("DB-HA-Manager 后端服务启动中...")
    yield
    # 关闭时执行
    logger.info("DB-HA-Manager 后端服务关闭中...")


# 创建 FastAPI 应用
app = FastAPI(
    title="DB-HA-Manager API",
    description="跨数据库高可用管理平台 API",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


def _parse_origin_list(raw_value: str) -> List[str]:
    """将逗号分隔的字符串转换为列表"""
    return [origin.strip() for origin in raw_value.split(",") if origin.strip()]


def _split_origins(origins: Sequence[str]) -> Tuple[List[str], List[str]]:
    """拆分字面量和包含通配符的来源"""
    literal_origins: List[str] = []
    wildcard_patterns: List[str] = []

    for origin in origins:
        if origin is None:
            continue
        cleaned = origin.strip()
        if not cleaned:
            continue
        if "*" in cleaned:
            escaped = re.escape(cleaned).replace("\\*", r"[^/:]+")
            wildcard_patterns.append(f"^{escaped}$")
        else:
            literal_origins.append(cleaned)

    return literal_origins, wildcard_patterns


def _determine_cors_origins() -> Tuple[List[str], Optional[re.Pattern[str]]]:
    """获取最终的 CORS allow_origins 和正则"""
    env_value = os.getenv("ALLOWED_ORIGINS")
    if env_value:
        origin_candidates = _parse_origin_list(env_value)
    else:
        origin_candidates = config_manager.app_config.allowed_origins or []

    if not origin_candidates:
        origin_candidates = DEFAULT_ALLOWED_ORIGINS.copy()

    literal_origins, wildcard_patterns = _split_origins(origin_candidates)

    origin_regex: Optional[re.Pattern[str]] = None
    if wildcard_patterns:
        combined_pattern = "|".join(wildcard_patterns)
        origin_regex = re.compile(combined_pattern)

    return literal_origins, origin_regex


allowed_origins, allowed_origin_regex = _determine_cors_origins()

cors_kwargs = {
    "allow_origins": allowed_origins,
    "allow_credentials": True,
    "allow_methods": ALLOWED_METHODS,
    "allow_headers": ALLOWED_HEADERS,
}

if allowed_origin_regex:
    cors_kwargs["allow_origin_regex"] = allowed_origin_regex

# 配置 CORS（默认限制到本地和内网）
app.add_middleware(
    CORSMiddleware,
    **cors_kwargs,
)

# 注册路由
app.include_router(health_router)
app.include_router(clusters_router)
app.include_router(tasks_router)
app.include_router(oracle_adg_router)


# 根路径
@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "DB-HA-Manager",
        "version": "0.1.0",
        "docs": "/api/docs",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
