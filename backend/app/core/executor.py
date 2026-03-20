"""执行器模块 - SSH 和 SQL 执行"""

import asyncio
import os
import shlex
import tempfile
import uuid
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False
    logger.warning("paramiko not available, SSH execution disabled")

from .config import SSHConfig


@dataclass
class CommandResult:
    """命令执行结果"""
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    execution_time: float = 0.0


class RemoteExecutor:
    """远程命令执行器 - 基于 SSH"""

    def __init__(self, ssh_config: SSHConfig):
        """
        初始化 SSH 执行器

        Args:
            ssh_config: SSH 连接配置
        """
        if not PARAMIKO_AVAILABLE:
            raise RuntimeError("paramiko 库未安装，无法使用 SSH 执行")

        self.config = ssh_config
        self.client: Optional[paramiko.SSHClient] = None
        self.last_error: Optional[Exception] = None

    def connect(self) -> bool:
        """
        建立 SSH 连接

        Returns:
            是否连接成功
        """
        self.last_error = None
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            # 准备连接参数
            timeout = max(self.config.connect_timeout or 0, 1)
            connect_kwargs = {
                "hostname": self.config.host,
                "port": self.config.port,
                "username": self.config.username,
                "timeout": timeout,
                "banner_timeout": timeout,
                "auth_timeout": timeout,
            }

            # 使用私钥或密码认证
            if self.config.private_key_path:
                private_key = self._load_private_key(self.config.private_key_path)
                connect_kwargs["pkey"] = private_key
            elif self.config.password:
                connect_kwargs["password"] = self.config.password

            self.client.connect(**connect_kwargs)
            logger.info(f"SSH 连接成功: {self.config.username}@{self.config.host}:{self.config.port}")
            return True

        except Exception as e:
            self.last_error = e
            logger.error(f"SSH 连接失败: {e}")
            return False

    def _load_private_key(self, key_path: str) -> paramiko.PKey:
        """加载私钥"""
        key_path = Path(key_path)
        key_type = self._detect_key_type(key_path)

        key_kwargs = {"filename": key_path}
        if self.config.private_key_password:
            key_kwargs["password"] = self.config.private_key_password

        if key_type == "RSA":
            return paramiko.RSAKey.from_private_key_file(**key_kwargs)
        elif key_type == "ECDSA":
            return paramiko.ECDSAKey.from_private_key_file(**key_kwargs)
        elif key_type == "ED25519":
            return paramiko.Ed25519Key.from_private_key_file(**key_kwargs)
        elif key_type == "DSA":
            return paramiko.DSSKey.from_private_key_file(**key_kwargs)
        else:
            raise ValueError(f"不支持的私钥类型: {key_type}")

    def _detect_key_type(self, key_path: Path) -> str:
        """检测私钥类型"""
        with open(key_path, "r") as f:
            first_line = f.readline().strip()

        key_markers = {
            "RSA": "-----BEGIN RSA PRIVATE KEY-----",
            "ECDSA": "-----BEGIN EC PRIVATE KEY-----",
            "ED25519": "-----BEGIN OPENSSH PRIVATE KEY-----",
            "DSA": "-----BEGIN DSA PRIVATE KEY-----",
        }

        for key_type, marker in key_markers.items():
            if marker in first_line or ("OPENSSH" in marker and marker in first_line):
                return key_type

        return "RSA"  # 默认

    def execute(
        self,
        command: str,
        environment: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None
    ) -> CommandResult:
        """
        执行远程命令

        Args:
            command: 要执行的命令
            environment: 环境变量字典
            timeout: 超时时间（秒）

        Returns:
            命令执行结果
        """
        if not self.client:
            raise RuntimeError("SSH 客户端未连接")

        try:
            import time
            start_time = time.time()

            # 构建完整命令（包含环境变量）
            full_command = self._build_command_with_env(command, environment)

            if timeout:
                quoted = shlex.quote(full_command)
                full_command = f"timeout {timeout}s bash -lc {quoted}"

            logger.debug(f"执行命令: {full_command[:200]}...")

            stdin, stdout, stderr = self.client.exec_command(
                full_command,
                get_pty=False,
                timeout=timeout
            )

            # 读取输出
            stdout_str = stdout.read().decode("utf-8", errors="ignore")
            stderr_str = stderr.read().decode("utf-8", errors="ignore")
            exit_code = stdout.channel.recv_exit_status()

            execution_time = time.time() - start_time

            result = CommandResult(
                success=exit_code == 0,
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=exit_code,
                execution_time=execution_time
            )

            if not result.success:
                logger.warning(f"命令执行失败 (exit_code={exit_code}): {stderr_str[:200]}")
            else:
                logger.debug(f"命令执行成功，耗时 {execution_time:.2f}s")

            return result

        except Exception as e:
            logger.error(f"命令执行异常: {e}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=str(e),
                exit_code=-1,
                execution_time=0.0
            )

    def _build_command_with_env(self, command: str, environment: Optional[Dict[str, str]] = None) -> str:
        """构建包含环境变量的命令"""
        if not environment:
            return command

        env_exports = " ".join([f'{k}="{v}"' for k, v in environment.items()])
        return f"export {env_exports} && {command}"

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """
        上传文件到远程服务器

        Args:
            local_path: 本地文件路径
            remote_path: 远程文件路径

        Returns:
            是否上传成功
        """
        if not self.client:
            raise RuntimeError("SSH 客户端未连接")

        try:
            sftp = self.client.open_sftp()
            sftp.put(local_path, remote_path)
            sftp.close()
            logger.info(f"文件上传成功: {local_path} -> {remote_path}")
            return True

        except Exception as e:
            logger.error(f"文件上传失败: {e}")
            return False

    def download_file(self, remote_path: str, local_path: str) -> bool:
        """
        从远程服务器下载文件

        Args:
            remote_path: 远程文件路径
            local_path: 本地文件路径

        Returns:
            是否下载成功
        """
        if not self.client:
            raise RuntimeError("SSH 客户端未连接")

        try:
            sftp = self.client.open_sftp()
            sftp.get(remote_path, local_path)
            sftp.close()
            logger.info(f"文件下载成功: {remote_path} -> {local_path}")
            return True

        except Exception as e:
            logger.error(f"文件下载失败: {e}")
            return False

    def close(self):
        """关闭 SSH 连接"""
        if self.client:
            self.client.close()
            self.client = None
            logger.debug("SSH 连接已关闭")

    def __enter__(self):
        """上下文管理器入口"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.close()


class SqlExecutor:
    """SQL 执行器 - SQL*Plus 封装"""

    def __init__(self, remote_executor: RemoteExecutor, oracle_home: str, oracle_sid: str):
        """
        初始化 SQL 执行器

        Args:
            remote_executor: SSH 执行器实例
            oracle_home: Oracle 安装路径
            oracle_sid: 数据库 SID
        """
        self.remote = remote_executor
        self.oracle_home = oracle_home
        self.oracle_sid = oracle_sid
        self.environment = {
            "ORACLE_HOME": oracle_home,
            "ORACLE_SID": oracle_sid,
            "PATH": f"{oracle_home}/bin:$PATH",
            "LD_LIBRARY_PATH": f"{oracle_home}/lib",
        }

    def execute_sql(
        self,
        sql: str,
        as_sysdba: bool = True,
        timeout: Optional[int] = None,
        environment: Optional[Dict[str, str]] = None,
        connect_identifier: Optional[str] = None
    ) -> CommandResult:
        """
        执行 SQL 语句
        """
        # 构建 SQL*Plus 连接命令
        if as_sysdba:
            sqlplus_cmd = 'sqlplus -s "/ as sysdba"'
        else:
            sqlplus_cmd = 'sqlplus -s "/"'
        if connect_identifier:
            sqlplus_cmd = f'sqlplus -s "{connect_identifier}"'

        # 构建 SQL 脚本内容（不包含 CONNECT，直接用 -s 连接）
        normalized_sql = sql.strip()
        if not normalized_sql.endswith(";"):
            normalized_sql = f"{normalized_sql};"
        sql_script = f"""SET HEADING OFF
SET FEEDBACK OFF
SET PAGESIZE 0
SET VERIFY OFF
SET ECHO OFF
SET TERMOUT ON
SET TRIMSPOOL ON
SET LINESIZE 32767
{normalized_sql}
EXIT
"""

        # 添加日志
        logger.debug(f"execute_sql SQL 脚本内容:\n{sql_script}")

        # 合并环境变量
        merged_env = dict(self.environment)
        if environment:
            merged_env.update(environment)

        local_temp_path = None
        remote_temp_path = f"/tmp/sql_exec_{uuid.uuid4().hex}.sql"
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as temp_file:
                temp_file.write(sql_script)
                local_temp_path = temp_file.name

            logger.debug(f"execute_sql 本地临时文件: {local_temp_path}")

            if not self.remote.upload_file(local_temp_path, remote_temp_path):
                logger.error("execute_sql 上传 SQL 文件失败")
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr=f"上传 SQL 文件失败: {local_temp_path} -> {remote_temp_path}",
                    exit_code=1,
                )

            full_command = f"{sqlplus_cmd} @{shlex.quote(remote_temp_path)}"
            logger.debug("execute_sql 使用 sqlplus @file 方式执行")

            result = self.remote.execute(
                full_command,
                environment=merged_env,
                timeout=timeout
            )

            logger.debug(f"execute_sql 原始 stdout: {repr((result.stdout or '')[:200])}")
            logger.debug(f"execute_sql 原始 stderr: {repr((result.stderr or '')[:200])}")
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error(f"execute_sql 执行异常: {exc}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=str(exc),
                exit_code=-1,
            )
        finally:
            if remote_temp_path:
                self.remote.execute(
                    f"rm -f {shlex.quote(remote_temp_path)}",
                    timeout=10
                )
            if local_temp_path:
                try:
                    os.unlink(local_temp_path)
                except FileNotFoundError:
                    pass

    def execute_sql_file(
        self,
        sql_file: str,
        as_sysdba: bool = True,
        timeout: Optional[int] = None
    ) -> CommandResult:
        """
        执行 SQL 脚本文件

        Args:
            sql_file: SQL 文件路径（远程服务器上的路径）
            as_sysdba: 是否以 SYSDBA 权限执行
            timeout: 超时时间（秒）

        Returns:
            执行结果
        """
        if as_sysdba:
            sqlplus_cmd = 'sqlplus -s "/ as sysdba"'
        else:
            sqlplus_cmd = 'sqlplus -s "/"'

        full_command = f"{sqlplus_cmd} @{shlex.quote(sql_file)}"

        result = self.remote.execute(
            full_command,
            environment=self.environment,
            timeout=timeout
        )
        return result

    def query_single_value(
        self,
        sql: str,
        as_sysdba: bool = True,
        timeout: Optional[int] = None
    ) -> Optional[str]:
        """
        查询单个值

        Args:
            sql: 查询 SQL
            as_sysdba: 是否以 SYSDBA 权限执行
            timeout: 超时时间（秒）

        Returns:
            查询结果值
        """
        result = self.execute_sql(sql, as_sysdba, timeout=timeout)

        if result.success:
            # 解析输出，获取第一行第一个值
            lines = [line.strip() for line in result.stdout.split("\n") if line.strip()]
            if lines:
                return lines[0]

        return None

    def query_multi_lines(
        self,
        sql: str,
        as_sysdba: bool = True,
        timeout: Optional[int] = None,
        environment: Optional[Dict[str, str]] = None
    ) -> List[str]:
        """
        查询多行结果

        Args:
            sql: 查询 SQL
            as_sysdba: 是否以 SYSDBA 权限执行
            timeout: 超时时间
            environment: 环境变量

        Returns:
            查询结果行列表（去除空行）
        """
        result = self.execute_sql(sql, as_sysdba, timeout, environment)

        if not result.success or not result.stdout:
            return []

        return self._extract_multi_lines(result.stdout)

    @staticmethod
    def _extract_multi_lines(output: str) -> List[str]:
        """
        从 SQL*Plus 输出中提取多行结果

        清理内容：
        - SQL*Plus banner 行
        - Copyright 行
        - Connected. / Disconnected from Oracle 行
        - SQL> 提示符
        - 空行
        """
        if not output:
            return []

        lines: List[str] = []
        keywords_to_skip = [
            "sql*plus:", "release", "production", "enterprise edition",
            "with partitioning", "olap", "data mining", "real application testing",
            "options", "copyright", "(c)", "connected.", "disconnected from",
            "sql>", "oracle database"
        ]

        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            line_lower = stripped.lower()
            if any(keyword in line_lower for keyword in keywords_to_skip):
                continue

            lines.append(stripped)

        return lines

    def query_to_dict(self, sql: str, as_sysdba: bool = True) -> List[Dict[str, Any]]:
        """
        查询并返回字典列表

        Args:
            sql: 查询 SQL
            as_sysdba: 是否以 SYSDBA 权限执行

        Returns:
            查询结果列表
        """
        result = self.execute_sql(sql, as_sysdba)

        if not result.success:
            return []

        # 解析输出（简单实现）
        lines = [line.strip() for line in result.stdout.split("\n") if line.strip()]
        # TODO: 实现完整的解析逻辑
        return []

    def execute_rman(
        self,
        rman_command: str,
        timeout: Optional[int] = None
    ) -> CommandResult:
        """
        执行 RMAN 命令

        Args:
            rman_command: RMAN 命令
            timeout: 超时时间（秒）

        Returns:
            执行结果
        """
        rman_cmd = f"rman target /"

        rman_script = f"""{rman_command}
EXIT
"""

        full_command = f'echo "{rman_script}" | {rman_cmd}'

        result = self.remote.execute(
            full_command,
            environment=self.environment,
            timeout=timeout
        )
        return result
