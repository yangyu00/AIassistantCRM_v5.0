# -*- coding: utf-8 -*-
"""远程文件源：SFTP（生产）与本地目录（离线测试 / 其他项目复用）。

``paramiko`` 只在 ``SftpRemoteSource.connect()`` 时延迟导入，模块级零第三方
依赖；``LocalDirRemoteSource`` 不装任何东西就能用。

连接生命周期：``connect()`` 惰性触发（``exists`` / ``fetch`` 会自动调），
``close()`` 后可再次 ``connect()``。编排器在每轮开始显式 ``connect()``，
连接失败记为整轮失败、不处理任何文件。

``exists()`` 三态：

- 成功 → ``True``
- ``FileNotFoundError`` → ``False``（编排器记 INFO「远程文件尚未生成」）
- 其他异常 → **透传**（编排器记 ERROR「stat 远程文件出错」）

后两者都跳过该文件、不登记清单；异常不在组件里吞掉。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .protocols import RemoteSource


class SftpRemoteSource:
    """生产 SFTP 源。

    :param sftp: ``SftpConfig``（或任何有 host/port/username/password 的对象）
    :param ssh_client: 可选注入，用于离线单测；不传则延迟 ``import paramiko``
    :param auto_add_policy: 配合 ``ssh_client`` 注入的 host key policy
    """

    def __init__(self, sftp, *, ssh_client=None, auto_add_policy=None):
        self._cfg = sftp
        self._injected_client = ssh_client
        self._injected_policy = auto_add_policy
        self._ssh = None
        self._sftp = None

    def _conf(self, key):
        conf = self._cfg
        return conf[key] if isinstance(conf, dict) else getattr(conf, key)

    # ---- 连接 ----
    def connect(self):
        """建立 SSH + SFTP 连接；幂等，已连接则直接返回。"""
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
    """把一个本地目录当「远程」，用于离线单测 / 其他项目本地导入。

    ``exists()`` / ``fetch()`` 只取 ``remote_path`` 的 **basename** 到
    ``source_dir`` 里找，所以编排器拼出的完整远程路径（如
    ``/ftpdata2/.../aifile/F09554YYYYMMDD000000.AVL``）无需改写就能用。
    ``connect()`` / ``close()`` 是 no-op，让编排器可以统一调用。
    """

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
    """装配工厂：给了 ``local_dir`` 走离线源，否则按 ``profile.sftp`` 连 SFTP。

    其他项目也可以直接构造两个实现类。
    """
    if local_dir is not None:
        return LocalDirRemoteSource(local_dir)
    return SftpRemoteSource(profile.sftp)


__all__ = ["SftpRemoteSource", "LocalDirRemoteSource", "build_remote_source"]
