"""远程文件源：SFTP（生产）与本地目录（离线）。"""
from __future__ import annotations

import shutil
from pathlib import Path

from .protocols import RemoteSource


class SftpRemoteSource:
    """生产 SFTP 源。"""

    def __init__(self, sftp, *, ssh_client=None, auto_add_policy=None):
        self._cfg = sftp
        self._injected_client = ssh_client
        self._injected_policy = auto_add_policy
        self._ssh = None
        self._sftp = None

    def _conf(self, key):
        conf = self._cfg
        return conf[key] if isinstance(conf, dict) else getattr(conf, key)

    def connect(self):
        """建立 SSH + SFTP 连接。"""
        if self._sftp is not None:
            return
        if self._injected_client is not None:
            ssh = self._injected_client
            policy = self._injected_policy
        else:
            import paramiko                      # 延迟导入：模块级零第三方依赖
            ssh = paramiko.SSHClient()
            policy = paramiko.AutoAddPolicy()

        ssh.set_missing_host_key_policy(policy)
        ssh.connect(
            self._conf("host"), int(self._conf("port")),
            self._conf("username"), self._conf("password"),
        )
        try:
            sftp = ssh.open_sftp()
        except Exception:
            try:
                ssh.close()
            except Exception:
                pass
            raise
        self._ssh, self._sftp = ssh, sftp

    def _ensure(self):
        if self._sftp is None:
            self.connect()

    def exists(self, remote_path: str) -> bool:
        self._ensure()
        try:
            self._sftp.stat(remote_path)
            return True
        except FileNotFoundError:
            return False
        # 其余异常（网络错误等）透传，由编排器记录为 ERROR 并跳过该文件

    def fetch(self, remote_path: str, local_path: Path) -> None:
        self._ensure()
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._sftp.get(remote_path, str(local_path))

    def close(self) -> None:
        """先关 sftp 再关 ssh，异常一律吞掉。"""
        for obj in (self._sftp, self._ssh):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self._sftp = self._ssh = None


class LocalDirRemoteSource:
    """把一个本地目录当「远程」，用于离线单测 / 其他项目本地导入。"""

    def __init__(self, source_dir):
        self.source_dir = Path(source_dir)

    def connect(self):
        return None

    def _local(self, remote_path: str) -> Path:
        return self.source_dir / Path(remote_path).name

    def exists(self, remote_path: str) -> bool:
        return self._local(remote_path).is_file()

    def fetch(self, remote_path: str, local_path: Path) -> None:
        src = self._local(remote_path)
        if not src.is_file():
            raise FileNotFoundError(f"本地源目录里没有 {src.name}: {src}")
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    def close(self):
        return None


def build_remote_source(profile, *, local_dir=None) -> RemoteSource:
    """装配工厂：给了 ``local_dir`` 走离线源，否则按 ``profile.sftp`` 连 SFTP。"""
    if local_dir is not None:
        return LocalDirRemoteSource(local_dir)
    return SftpRemoteSource(profile.sftp)


__all__ = ["SftpRemoteSource", "LocalDirRemoteSource", "build_remote_source"]
