"""数据库高可用驱动基类"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Callable


class DatabaseHADriver(ABC):
    """
    数据库高可用驱动抽象基类

    所有数据库（Oracle、MySQL、SQL Server、PostgreSQL）的 HA 驱动
    必须继承此类并实现所有抽象方法。
    """

    def __init__(self, cluster_id: str, config: Dict[str, Any]):
        """
        初始化驱动

        Args:
            cluster_id: 集群 ID
            config: 集群配置
        """
        self.cluster_id = cluster_id
        self.config = config

    @abstractmethod
    def setup(self, config: Dict[str, Any]) -> Callable[..., Dict[str, Any]]:
        """
        搭建主备架构

        Args:
            config: 搭建配置参数

        Returns:
            可由任务管理器执行的可调用对象
        """
        pass

    @abstractmethod
    def status(self) -> Dict[str, Any]:
        """
        获取主备状态

        Returns:
            包含主备状态信息的字典
        """
        pass

    @abstractmethod
    def switchover(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        执行 Switchover（正常切换）

        Args:
            dry_run: 是否为演练模式（仅检查可行性，不实际执行）

        Returns:
            切换结果字典
        """
        pass

    @abstractmethod
    def failover(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        执行 Failover（应急接管）

        Args:
            dry_run: 是否为演练模式（模拟故障，不实际激活备库）

        Returns:
            接管结果字典
        """
        pass

    @abstractmethod
    def manage(self, action: str, **kwargs) -> Any:
        """
        执行其他管理操作

        Args:
            action: 操作类型（如 'sync', 'cleanup_archive', 'backup' 等）
            **kwargs: 操作参数

        Returns:
            操作结果
        """
        pass
