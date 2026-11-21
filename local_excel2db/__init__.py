# -*- coding: utf-8 -*-
"""``local_excel2db``：本地 Excel 维表 → SQLite 的**写入侧**独立进程。

只做一件事：定时轮询项目根 ``excels/`` 下的注册 xlsx，内容指纹变化则抽列、
单事务整表覆盖 ``local_excel2db/runtime/sqlite_db/dimensions.db``、按表递增
version。本包不含任何在线读取 / 查库 / 业务 dict 重建 / 话术加工能力
（``reader.py`` / ``transform.py`` 与对应的契约、配置声明一并移除）；在线只读侧
已分离到 ``services.dimensions``，按 ``product_id`` 读本包产出的库：取商机
标签 ID，并在诊断失败时兜底取产品名称 /「匹配结果2」/「标签N」话术；
两侧只共用 ``registry.json`` 的列契约，不共用代码。

与 ``IOP`` / ``sftp_avl2db`` / ``call_stat`` 一致：**配置与运行时产出都锚在
包内、按 ``__file__`` 定位，与启动 CWD 无关**。配置唯一来源是包内
``registry.json``（唯一入口 ``load_registry()``），输入恒为项目根 ``excels/``，
产物恒为包内 ``runtime/``；不读系统环境变量，也没有 ``--config`` / 外部路径覆盖。

``import local_excel2db`` 零 I/O：不读 registry、不连库、不建目录、不改全局
logger、不拉起 openpyxl / sqlite3。只有显式调用 ``load_registry()`` 或运行
``python -m local_excel2db`` 才产生 I/O。

    python -m local_excel2db                 # 常驻轮询，间隔由包内 registry 控制
    python -m local_excel2db --once         # 只检测一轮后退出
    python -m local_excel2db --once --force # 忽略指纹强制重导并递增 version

分层：``config`` / ``models``（静态）→ ``excel_source``（源文件解析）/
``repository``（SQLite 写入）→ ``importer``（导入编排）→ ``watcher`` / ``cli``
（守护与命令行）。契约见 ``protocols.py``，各层可单独替换。
"""
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
