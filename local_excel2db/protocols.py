# -*- coding: utf-8 -*-
"""组件契约与结果对象（纯 typing，零 I/O、零第三方依赖）。

只约束方法形状，不约束实现技术；各层通过构造器注入，可换成测试替身或宿主
项目自己的实现：

    ExcelSource            注册 xlsx 的指纹与抽列
    ReadableRepository     只读查询（读取侧替身只需实现 query / close）
    WritableRepository     建表 / 指纹 / 整表覆盖（写入侧替身不需实现 query）
    DimensionRepository    读写合一（默认 Sqlite 实现）
    InstanceLock           单机单实例锁
    LogSink                日志出口
    Sleeper                轮询休眠（可替换成立即返回）
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # 避免运行时循环导入；models 不依赖本模块
    from .models import TableSpec

Row = tuple


@dataclass(frozen=True)
class FileFingerprint:
    """注册文件的内容指纹：以 sha256 为准，size / mtime 仅作元数据留存。"""

    sha256: str
    size: int
    mtime: float


@dataclass(frozen=True)
class ParsedRows:
    """一次抽列结果：已按主键去重、按 columns 顺序 cast 好的行值。"""

    rows: tuple[Row, ...]
    skipped_empty: int = 0
    duplicated: int = 0
    header: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportResult:
    """单张表一轮导入的结果；不写日志，由编排器决定如何记录。"""

    table: str
    status: str                 # "missing" | "unchanged" | "updated"
    version: int = 0
    rows: int = 0
    skipped_empty: int = 0
    duplicated: int = 0
    sha256: str = ""
    path: str = ""


class MissingColumn(Exception):
    """必需表头列缺失；编排器记录并跳过本轮，下轮重试。"""


class SchemaMismatch(RuntimeError):
    """库表列集合与 registry 声明不一致；提示删库重建，不静默写错列。"""


@runtime_checkable
class ExcelSource(Protocol):
    """注册 xlsx 的读取侧：指纹 + 抽列。"""

    def fingerprint(self, path: Path) -> FileFingerprint: ...

    def read_rows(self, spec: TableSpec, path: Path) -> ParsedRows: ...


@runtime_checkable
class ReadableRepository(Protocol):
    """只读查询契约：``DimensionReader`` 只需要这两个方法。"""

    def query(self, table: str, columns: Sequence[str],
              *, order_by_rowid: bool = False) -> list[Row]: ...

    def close(self) -> None: ...


@runtime_checkable
class WritableRepository(Protocol):
    """写入契约：``TableImporter`` 只需要这四个方法 + close。"""

    def ensure_meta(self) -> None: ...

    def ensure_table(self, spec: TableSpec) -> None: ...

    def previous_version(self, spec: TableSpec) -> tuple[int, str] | None: ...

    def replace_all(self, spec: TableSpec, parsed: ParsedRows, version: int,
                    fingerprint: FileFingerprint, source_path: Path) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class DimensionRepository(ReadableRepository, WritableRepository, Protocol):
    """读写合一契约；默认的 ``SqliteDimensionRepository`` 实现全部方法。"""


@runtime_checkable
class InstanceLock(Protocol):
    """单机单实例锁；acquire 失败返回 False 而不是抛异常。"""

    def acquire(self) -> bool: ...

    def release(self) -> None: ...


@runtime_checkable
class LogSink(Protocol):
    """日志出口；宿主项目可直接传 logging.Logger。"""

    def info(self, msg: str) -> None: ...

    def warning(self, msg: str) -> None: ...

    def error(self, msg: str) -> None: ...

    def exception(self, msg: str) -> None: ...


Sleeper = Callable[[float], None]


__all__ = [
    "Row", "FileFingerprint", "ParsedRows", "ImportResult",
    "MissingColumn", "SchemaMismatch",
    "ExcelSource", "ReadableRepository", "WritableRepository",
    "DimensionRepository", "InstanceLock", "LogSink", "Sleeper",
]
