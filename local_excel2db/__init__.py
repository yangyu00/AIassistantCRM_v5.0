# -*- coding: utf-8 -*-
"""``local_excel2db``：本地 Excel 维表 → SQLite 的**写入侧**独立进程。"""
from .config import (
    MODULE_DIR, PROJECT_ROOT, INPUT_DIR, REGISTRY_PATH, RUNTIME_DIR,
    DB_PATH, STATE_DIR, LOCK_PATH, LOG_DIR, load_registry,
)
from .models import ColumnSpec, Registry, TableSpec
from .protocols import (
    ExcelSource, FileFingerprint, ImportResult, InstanceLock, LogSink,
    MissingColumn, ParsedRows, SchemaMismatch, Sleeper, WritableRepository,
)

# 延迟导出：(模块名, 属性名)。import 本包不加载这些模块，只有 from ... import
# 或调用时才加载，保证零 I/O。
_LAZY_EXPORTS = {
    "OpenpyxlExcelSource": ("excel_source", "OpenpyxlExcelSource"),
    "SqliteDimensionRepository": ("repository", "SqliteDimensionRepository"),
    "TableImporter": ("importer", "TableImporter"),
    "SingleInstanceLock": ("watcher", "SingleInstanceLock"),
    "build_logger": ("watcher", "build_logger"),
    "DimensionWatcher": ("watcher", "DimensionWatcher"),
    "AssembledWatcher": ("watcher", "AssembledWatcher"),
    "build_watcher": ("watcher", "build_watcher"),
    "cli_main": ("cli", "main"),
    "build_arg_parser": ("cli", "build_arg_parser"),
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
    # 路径常量与唯一配置入口（全部按 __file__ 定位）
    "MODULE_DIR", "PROJECT_ROOT", "INPUT_DIR", "REGISTRY_PATH", "RUNTIME_DIR",
    "DB_PATH", "STATE_DIR", "LOCK_PATH", "LOG_DIR", "load_registry",
    # 配置与模型
    "Registry", "TableSpec", "ColumnSpec",
    # 契约与结果对象
    "ExcelSource", "WritableRepository", "InstanceLock", "LogSink", "Sleeper",
    "FileFingerprint", "ParsedRows", "ImportResult",
    "MissingColumn", "SchemaMismatch",
    # 写入侧组件（延迟）
    "OpenpyxlExcelSource", "SqliteDimensionRepository", "TableImporter",
    "SingleInstanceLock", "build_logger", "DimensionWatcher",
    "AssembledWatcher", "build_watcher",
    # CLI（延迟）
    "cli_main", "build_arg_parser",
]
