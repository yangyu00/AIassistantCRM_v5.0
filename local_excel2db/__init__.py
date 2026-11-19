# -*- coding: utf-8 -*-
"""``local_excel2db``：本地 Excel 维表 → SQLite，读写分离、可独立复用。

**import 本包零 I/O、零第三方依赖**：不读 registry、不连库、不建目录、不改全局
logger、不拉起 openpyxl / sqlite3。配置只在显式调用时加载。

在线业务（``main.py``）一行接入::

    from local_excel2db import load_product_selling_point
    points = load_product_selling_point()      # 任何故障降级为 {}

写入侧进程::

    python -m local_excel2db --once
    python -m local_excel2db --once --force --config /path/to/registry.json

其他项目按对象接入（相对路径一律相对 registry 文件所在目录解析）::

    from local_excel2db import RegistryLoader, SqliteDimensionRepository, DimensionReader

    reg = RegistryLoader().from_file("my_registry.json")
    reader = DimensionReader(reg, SqliteDimensionRepository(reg, readonly=True))
    points = reader.load_product_selling_point()

分层：``config`` / ``models``（静态）→ ``excel_source`` / ``repository``（I/O）→
``importer``（写入编排）→ ``reader`` / ``transform``（只读与重组）→
``watcher`` / ``cli``（守护与命令行）。契约见 ``protocols.py``，各层可单独替换。
"""
from .config import RegistryLoader
from .models import (
    ColumnSpec, CutInScriptSpec, LookupSpec, Registry, TableSpec,
)
from .protocols import (
    DimensionRepository, ExcelSource, FileFingerprint, ImportResult,
    InstanceLock, LogSink, MissingColumn, ParsedRows, ReadableRepository,
    SchemaMismatch, Sleeper, WritableRepository,
)

# 延迟导出：(模块名, 属性名)。import 本包不加载这些模块，只有 from ... import
# 或调用下面的便捷函数时才加载，保证零 I/O。
_LAZY_EXPORTS = {
    "OpenpyxlExcelSource": ("excel_source", "OpenpyxlExcelSource"),
    "SqliteDimensionRepository": ("repository", "SqliteDimensionRepository"),
    "TableImporter": ("importer", "TableImporter"),
    "cut_in_str2tuple": ("transform", "cut_in_str2tuple"),
    "build_cut_in_script_dict": ("transform", "build_cut_in_script_dict"),
    "CutInScriptTransformer": ("transform", "CutInScriptTransformer"),
    "DimensionReader": ("reader", "DimensionReader"),
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


def load_product_selling_point() -> dict:
    """在线业务一行接入：``{business_id: (product_name, selling_point, label_id)}``。

    每次调用重新读一次 registry 与全表（不做进程级缓存），因此天然读到写入侧的
    最新值；registry 缺失/损坏、库/表/列未就绪或查询失败时返回 ``{}``，业务按
    「无维表」降级，绝不会因维表问题抛异常。
    """
    from .reader import DimensionReader
    return DimensionReader.from_registry_file().load_product_selling_point()


__all__ = [
    # 配置与模型
    "RegistryLoader", "Registry", "TableSpec", "ColumnSpec",
    "LookupSpec", "CutInScriptSpec",
    # 契约与结果对象
    "ExcelSource", "ReadableRepository", "WritableRepository",
    "DimensionRepository", "InstanceLock", "LogSink", "Sleeper",
    "FileFingerprint", "ParsedRows", "ImportResult",
    "MissingColumn", "SchemaMismatch",
    # 写入侧（延迟）
    "OpenpyxlExcelSource", "SqliteDimensionRepository", "TableImporter",
    "SingleInstanceLock", "build_logger", "DimensionWatcher",
    "AssembledWatcher", "build_watcher",
    # 读取侧（延迟）
    "DimensionReader", "CutInScriptTransformer",
    "cut_in_str2tuple", "build_cut_in_script_dict",
    # 在线业务接入
    "load_product_selling_point",
    # CLI（延迟）
    "cli_main", "build_arg_parser",
]
