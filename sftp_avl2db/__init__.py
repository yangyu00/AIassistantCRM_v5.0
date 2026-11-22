# -*- coding: utf-8 -*-
"""``sftp_avl2db``：SFTP AVL → SQLite 入库包。"""
from . import avl_specs, table_schemas   # noqa: E402  (纯静态契约)
from .config import (
    ENV_PATH,
    RUNTIME_DIR,
    RetentionConfig,
    SftpConfig,
    TaskProfileConfig,
    iter_remote_names,
    load_profiles,
    remote_name_for,
)
from .protocols import (
    ImportResult,
    ImportStatus,
    ParseStats,
    RecordParser,
    SinkStats,
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
    "TaskLogger": ("task_logging", "TaskLogger"),
    "InMemoryLogger": ("task_logging", "InMemoryLogger"),
    "build_logger": ("task_logging", "build_logger"),
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
    "avl_specs", "table_schemas", "ENV_PATH", "RUNTIME_DIR",
    "SftpConfig", "RetentionConfig",
    "TaskProfileConfig", "load_profiles", "iter_remote_names", "remote_name_for",
    # 契约
    "ParseStats", "ImportResult", "ImportStatus", "SinkStats",
    "RemoteSource", "RecordParser",
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
