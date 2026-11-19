# -*- coding: utf-8 -*-
"""状态层：断点续传清单（``FetchedManifest``）与单实例锁（``SingleInstanceLock``）。

从 ``tasks/base_task.py`` 的两个类逐行照搬，变化只有三处：

1. **路径由构造参数注入**，不再用 ``dirname(dirname(__file__))`` 拼死目录；
   阶段 1 的 ``TaskProfileConfig.fetched_path`` / ``.lock_path`` 直接传进来即可。
2. ``portalocker`` / ``fcntl`` 改成模块级**可选**导入变量：
   装了 portalocker 优先用，否则退化 fcntl，两者都没有时明确报
   ``RuntimeError``（旧代码在 Windows 上会直接 ``import fcntl`` 崩掉）。
   变量形如 ``_portalocker`` / ``_fcntl``，测试可 ``mock.patch`` 强制走某一分支。
3. 锁文件用 ``pathlib`` 兼容 ``Path`` 入参；JSON 字段
   ``pid / host / start_time`` 与写入顺序逐字未改。

**没有改的语义**：清单「追加写 + flush + fsync + 内存 set」，重复 add 不落盘，
启动时读回（不做每轮 reload，与旧代码一致）；锁非阻塞、拿不到立即 False，
acquire 成功后写 pid/host/start_time，release 解锁并关闭、可重复调用。

``portalocker`` 的 import 用 ``try/except``，缺库不会让 ``import sftp_avl2db.state``
失败；未安装时 Python 也不会把它留在 ``sys.modules`` 里。
"""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from pathlib import Path

from .protocols import StateStore

try:
    import portalocker as _portalocker          # 可选依赖，有则优先用
except Exception:                              # pragma: no cover - 取决于环境
    _portalocker = None

try:
    import fcntl as _fcntl
except ImportError:                            # pragma: no cover - Windows
    _fcntl = None


class FetchedManifest:
    """已成功入库的远程文件名清单（断点续传 / 不重复拉取）。

    以「追加写 + 行级 flush + fsync」落盘，进程被 kill 也不会丢失已完成记录；
    每行一个文件名，构造时读入内存 set。
    """

    def __init__(self, manifest_path):
        self.manifest_path = Path(manifest_path)
        self._done = set()
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        if self.manifest_path.is_file():
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                for line in f:
                    name = line.strip()
                    if name:
                        self._done.add(name)

    def has(self, remote_filename: str) -> bool:
        return remote_filename in self._done

    def add(self, remote_filename: str) -> None:
        if remote_filename in self._done:
            return
        with open(self.manifest_path, "a", encoding="utf-8") as f:
            f.write(remote_filename + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._done.add(remote_filename)

    def __len__(self):
        return len(self._done)

    def __iter__(self):
        """按字典序迭代，便于诊断 / 比对（不改变内部 set 语义）。"""
        return iter(sorted(self._done))

    def __contains__(self, remote_filename):
        return remote_filename in self._done


class SingleInstanceLock:
    """单机单实例文件锁。拿不到锁立即失败（非阻塞），避免两个拉取者并发出库。"""

    def __init__(self, lock_path):
        self.lock_path = Path(lock_path)
        self._fh = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "w", encoding="utf-8")
        try:
            if _portalocker is not None:
                _portalocker.lock(
                    self._fh, _portalocker.LOCK_EX | _portalocker.LOCK_NB)
            elif _fcntl is not None:
                _fcntl.flock(
                    self._fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            else:
                raise RuntimeError(
                    "当前平台没有可用的文件锁：请安装 portalocker，或在 Unix 上运行")
        except Exception:
            self._fh.close()
            self._fh = None
            return False
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(json.dumps({
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False))
        self._fh.flush()
        return True

    def release(self):
        if self._fh is None:
            return
        try:
            if _portalocker is not None:
                _portalocker.unlock(self._fh)
            elif _fcntl is not None:
                _fcntl.flock(self._fh.fileno(), _fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def build_state(profile):
    """按 ``TaskProfileConfig`` 装配状态层（阶段 5 编排器用）。

    返回 ``(manifest, lock)``：路径全部来自 profile，不再有硬编码目录。
    """
    return (FetchedManifest(profile.fetched_path),
            SingleInstanceLock(profile.lock_path))


__all__ = ["FetchedManifest", "SingleInstanceLock", "build_state", "StateStore"]
