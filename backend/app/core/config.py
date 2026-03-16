"""配置管理模块 - Pydantic + YAML + 加密"""

import os
import logging
import yaml
from pathlib import Path
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field, validator
from cryptography.fernet import Fernet
import base64


logger = logging.getLogger(__name__)


DEFAULT_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:8080",
    "http://192.168.*:3000",
    "http://192.168.*:8080",
]


class SSHConfig(BaseModel):
    """SSH 连接配置"""
    host: str
    port: int = 22
    username: str
    password: Optional[str] = Field(default=None, description="密码或私钥密码")
    private_key_path: Optional[str] = Field(default=None, description="私钥路径")
    private_key_password: Optional[str] = Field(default=None, description="私钥密码")
    connect_timeout: int = 30


class OracleConfig(BaseModel):
    """Oracle 连接配置"""
    sid: Optional[str] = None
    service_name: Optional[str] = None
    sys_password: Optional[str] = None  # 加密存储
    oracle_home: Optional[str] = None
    oracle_sid: Optional[str] = None
    db_unique_name: Optional[str] = None
    oracle_base: Optional[str] = None
    listener_port: Optional[int] = Field(default=1521, ge=1, le=65535)
    storage_type: Optional[str] = None
    is_cdb: Optional[bool] = None


class ClusterConfig(BaseModel):
    """集群配置"""
    cluster_id: str
    cluster_name: str
    db_type: str = "oracle"  # oracle, mysql, sqlserver, postgresql
    primary: OracleConfig
    primary_ssh: SSHConfig
    standby: OracleConfig
    standby_ssh: SSHConfig
    data_files_path: str
    archivelog_path: str


class AppConfig(BaseModel):
    """应用配置"""
    app_name: str = "DB-HA-Manager"
    app_version: str = "0.1.0"
    debug: bool = False
    log_level: str = "INFO"

    # 加密密钥（从环境变量或文件读取）
    encryption_key: Optional[str] = None

    # 数据库存储配置
    database_url: str = "sqlite:///./db_ha_manager.db"

    # 任务配置
    task_timeout: int = 3600  # 1小时
    max_concurrent_tasks: int = 5
    task_store_path: str = "data/tasks.json"
    allowed_origins: List[str] = Field(default_factory=lambda: DEFAULT_ALLOWED_ORIGINS.copy())

    @validator("encryption_key", pre=True)
    def validate_encryption_key(cls, v):
        """验证加密密钥"""
        if v is None:
            return v
        if isinstance(v, bytes):
            v = v.decode()
        v = v.strip()
        return v or None

    @validator("allowed_origins", pre=True)
    def validate_allowed_origins(cls, value):
        """确保允许的来源配置格式正确"""
        if value is None:
            return DEFAULT_ALLOWED_ORIGINS.copy()

        if isinstance(value, str):
            items = [item.strip() for item in value.split(",")]
        elif isinstance(value, (list, tuple, set)):
            items = []
            for item in value:
                if item is None:
                    continue
                cleaned = str(item).strip()
                if cleaned:
                    items.append(cleaned)
        else:
            raise ValueError("allowed_origins must be a list or comma-separated string")

        cleaned_items: List[str] = []
        seen = set()
        for entry in items:
            if not entry:
                continue
            if entry not in seen:
                cleaned_items.append(entry)
                seen.add(entry)

        return cleaned_items or DEFAULT_ALLOWED_ORIGINS.copy()


class ConfigManager:
    """配置管理器"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = Path(config_path)
        self.encryption_key: Optional[bytes] = None
        self.cipher: Optional[Fernet] = None
        self.app_config: AppConfig = AppConfig()
        self.clusters: Dict[str, ClusterConfig] = {}

        self._load_config()

    def _load_config(self):
        """加载配置文件"""
        if not self.config_path.exists():
            self._create_default_config()
            return

        with open(self.config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        if config_data:
            # 加载应用配置
            app_data = config_data.get("app", {})
            self.app_config = AppConfig(**app_data)

            # 初始化加密
            self._init_cipher(self.app_config.encryption_key)

            # 加载集群配置
            clusters_data = config_data.get("clusters", {})
            for cluster_id, cluster_data in clusters_data.items():
                cluster_data["cluster_id"] = cluster_id
                self.clusters[cluster_id] = ClusterConfig(**cluster_data)

    def _init_cipher(self, key: Optional[str]):
        """初始化加密器"""
        env_key = os.getenv("ENCRYPTION_KEY")
        key_source: Optional[str] = None

        def _normalize_to_bytes(value: Optional[Any]) -> Optional[bytes]:
            if value is None:
                return None
            if isinstance(value, bytes):
                cleaned = value.strip()
                return cleaned or None
            if isinstance(value, str):
                cleaned = value.strip()
                return cleaned.encode() if cleaned else None
            return None

        candidate_bytes = None
        if env_key:
            candidate_bytes = _normalize_to_bytes(env_key)
            if candidate_bytes:
                key_source = "environment variable ENCRYPTION_KEY"
        if candidate_bytes is None and key:
            candidate_bytes = _normalize_to_bytes(key)
            if candidate_bytes:
                key_source = "config file (app.encryption_key)"

        if candidate_bytes:
            try:
                cipher = Fernet(candidate_bytes)
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid encryption key found in %s. Generating a new key; set a valid 32-byte base64 key.",
                    key_source,
                )
            else:
                self.encryption_key = candidate_bytes
                self.cipher = cipher
                return

        generated_key = Fernet.generate_key()
        logger.warning(
            "Encryption key not provided. Generated a new key for this process; configure ENCRYPTION_KEY "
            "or app.encryption_key in config.yaml to persist it."
        )
        self.encryption_key = generated_key
        self.cipher = Fernet(self.encryption_key)

    def _create_default_config(self):
        """创建默认配置文件"""
        default_config = {
            "app": {
                "app_name": "DB-HA-Manager",
                "app_version": "0.1.0",
                "debug": False,
                "log_level": "INFO",
                "database_url": "sqlite:///./db_ha_manager.db",
                "task_store_path": "data/tasks.json",
                "allowed_origins": DEFAULT_ALLOWED_ORIGINS.copy(),
            },
            "clusters": {
                "example": {
                    "cluster_name": "示例集群",
                    "db_type": "oracle",
                    "primary": {
                        "sid": "ORCL",
                        "oracle_home": "/u01/app/oracle/product/19c/dbhome_1",
                        "oracle_sid": "ORCL",
                    },
                    "primary_ssh": {
                        "host": "192.168.1.10",
                        "port": 22,
                        "username": "oracle",
                        "private_key_path": "/home/oracle/.ssh/id_rsa",
                    },
                    "standby": {
                        "sid": "ORCL",
                        "oracle_home": "/u01/app/oracle/product/19c/dbhome_1",
                        "oracle_sid": "ORCL",
                    },
                    "standby_ssh": {
                        "host": "192.168.1.11",
                        "port": 22,
                        "username": "oracle",
                        "private_key_path": "/home/oracle/.ssh/id_rsa",
                    },
                    "data_files_path": "/u01/oradata/ORCL",
                    "archivelog_path": "/u01/oradata/ORCL/archivelog",
                }
            }
        }

        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(default_config, f, allow_unicode=True, default_flow_style=False)

    def encrypt(self, plain_text: str) -> str:
        """加密文本"""
        if not self.cipher:
            raise RuntimeError("加密器未初始化")
        encrypted = self.cipher.encrypt(plain_text.encode())
        return base64.urlsafe_b64encode(encrypted).decode()

    def decrypt(self, encrypted_text: str) -> str:
        """解密文本"""
        if not self.cipher:
            raise RuntimeError("加密器未初始化")
        encrypted = base64.urlsafe_b64decode(encrypted_text.encode())
        decrypted = self.cipher.decrypt(encrypted)
        return decrypted.decode()

    def save_cluster(self, cluster: ClusterConfig):
        """保存或更新集群配置"""
        self.clusters[cluster.cluster_id] = cluster
        self._save_config()

    def delete_cluster(self, cluster_id: str):
        """删除集群配置"""
        if cluster_id in self.clusters:
            del self.clusters[cluster_id]
            self._save_config()

    def get_cluster(self, cluster_id: str) -> Optional[ClusterConfig]:
        """获取集群配置"""
        return self.clusters.get(cluster_id)

    def list_clusters(self) -> Dict[str, ClusterConfig]:
        """获取所有集群配置"""
        return self.clusters

    def _save_config(self):
        """保存配置到文件"""
        config_data = {
            "app": self.app_config.dict(),
            "clusters": {
                cluster_id: cluster.dict(exclude={"cluster_id"})
                for cluster_id, cluster in self.clusters.items()
            }
        }

        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, allow_unicode=True, default_flow_style=False)


# 全局配置管理器实例
config_manager = ConfigManager()
