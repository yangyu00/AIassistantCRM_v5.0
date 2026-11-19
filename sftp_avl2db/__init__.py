# -*- coding: utf-8 -*-
"""``sftp_avl2db``：SFTP AVL → SQLite 入库包。

**配置与运行时产出都在包内，都用 ``__file__`` 定位，与 CWD 无关**：

- ``sftp_avl2db/.env``：唯一配置来源，改完重启进程；不读系统环境变量
- ``sftp_avl2db/runtime/``：``sqlite_db`` / ``state`` / ``files`` / ``log``，
  写入侧产出、在线侧只读；可换成指向数据盘的软链

唯一配置入口是 ``load_profiles()``：

    from sftp_avl2db import load_profiles, build_task
    profile = load_profiles()["loss_control"]     # 不传参 = 包内默认，读写同源
    task = build_task(profile)
    task.runner.serve_forever(only_date="20261110")

把包 copy 到别的项目时，请**排除 ``.env`` 与 ``runtime/``**，从
``.env.example`` 起步，避免把 A 项目的凭据和业务库一起带走。

``import sftp_avl2db`` 零 I/O、零第三方依赖：不读 ``.env``、不连网络、
不建目录、不写日志、不改全局 logger。配置只在显式调用 ``load_profiles()``
时加载。

五个组件层可单独 import、单独测试、单独替换：

| 层 | 模块 | 顶层第三方依赖 |
|---|---|---|
| 配置 | ``config`` / ``dbspec`` | 无 |
| 解析 | ``parsers`` | 无 |
| 写入 | ``sink`` | 无（只用标准库 sqlite3） |
| I/O | ``remote`` / ``state`` / ``logging`` | 无（延迟导入 paramiko / loguru） |
| 编排 | ``runner`` / ``profiles`` / ``cli`` | 无 |

带可选依赖的类（``SftpRemoteSource`` / ``TaskLogger`` / ``SingleInstanceLock``）
通过模块级 ``__getattr__`` **延迟导出**：``import sftp_avl2db`` 完全不碰它们，
真正 ``from sftp_avl2db import build_task`` 时才加载对应模块。
"""
from . import dbspec                      # noqa: E402  (纯静态契约)
from .config import (
    ENV_PATH,
    RUNTIME_DIR,
    RetentionConfig,
    SftpConfig,
    TaskProfileConfig,
    iter_remote_names,
    load_profiles,
)
from .protocols import (
    ImportResult,
    ParseStats,
    RecordParser,
    RecordSink,
    RemoteSource,
    StateStore,
    TableLayout,
)

# 延迟导出表：(模块名, 属性名)。只在被 from ... import 时才加载模块，
# 避免单纯 import sftp_avl2db 触发 state.py 的可选依赖探测。
_LAZY_EXPORTS = {
    "Avl2DbRunner": ("runner", "Avl2DbRunner"),
    "AssembledTask": ("profiles", "AssembledTask"),
    "build_task": ("profiles", "build_task"),
    "build_all": ("profiles", "build_all"),
    "cli_main": ("cli", "main"),
    "build_arg_parser": ("cli", "build_arg_parser"),
    "TaskLogger": ("logging", "TaskLogger"),
    "InMemoryLogger": ("logging", "InMemoryLogger"),
    "build_logger": ("logging", "build_logger"),
    "SftpRemoteSource": ("remote", "SftpRemoteSource"),
    "LocalDirRemoteSource": ("remote", "LocalDirRemoteSource"),
    "build_remote_source": ("remote", "build_remote_source"),
    "FetchedManifest": ("state", "FetchedManifest"),
    "SingleInstanceLock": ("state", "SingleInstanceLock"),
    "build_state": ("state", "build_state"),
    "FlatColumnParser": ("parsers", "FlatColumnParser"),
    "ContinuationParser": ("parsers", "ContinuationParser"),
    "build_parser": ("parsers", "build_parser"),
    "TableSchema": ("sink", "TableSchema"),
    "SqliteSink": ("sink", "SqliteSink"),
    "build_layout": ("sink", "build_layout"),
    "build_sink": ("sink", "build_sink"),
}


def __getattr__(name):
    if name in _LAZY_EXPORTS:
        import importlib
        mod, attr = _LAZY_EXPORTS[name]
        return getattr(importlib.import_module("." + mod, __name__), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    # 配置（唯一入口 load_profiles()，其余是它产出的冻结对象与常量）
    "dbspec", "ENV_PATH", "RUNTIME_DIR", "SftpConfig", "RetentionConfig",
    "TaskProfileConfig", "load_profiles", "iter_remote_names",
    # 契约
    "ParseStats", "ImportResult", "RemoteSource", "RecordParser",
    "TableLayout", "RecordSink", "StateStore",
    # 组件（延迟）
    "FlatColumnParser", "ContinuationParser", "build_parser",
    "TableSchema", "SqliteSink", "build_layout", "build_sink",
    "SftpRemoteSource", "LocalDirRemoteSource", "build_remote_source",
    "FetchedManifest", "SingleInstanceLock", "build_state",
    "TaskLogger", "InMemoryLogger", "build_logger",
    # 编排（延迟）
    "Avl2DbRunner", "AssembledTask", "build_task", "build_all",
    "cli_main", "build_arg_parser",
]
