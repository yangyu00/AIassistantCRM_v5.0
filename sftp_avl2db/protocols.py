"""组件契约（纯 typing，零 I/O）：各层组件的 Protocol 与结果数据类。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable, Iterator, Literal, Protocol, runtime_checkable

Row = tuple  # 逻辑记录元组；列数由 TableSchema.validate_row() 校验


class ImportStatus:
    """``ImportResult.status`` 的取值，避免散落的裸字符串。"""

    SKIP: Final = "skip"
    MISSING: Final = "missing"
    FAILED: Final = "failed"
    OK: Final = "ok"


@dataclass
class ParseStats:
    """单文件解析的统计信号。"""

    rows: int = 0
    skipped: int = 0
    encoding: str = ""
    malformed_heads: int = 0        # 控损：起始行列数 < 4，整条丢弃
    orphan_continuations: int = 0   # 控损：文件开头尚无记录时的孤立续行

    def reset(self) -> None:
        self.rows = 0
        self.skipped = 0
        self.encoding = ""
        self.malformed_heads = 0
        self.orphan_continuations = 0


@runtime_checkable
class RemoteSource(Protocol):
    """远程（或伪远程）文件源。"""

    def connect(self) -> None: ...

    def exists(self, remote_path: str) -> bool: ...

    def fetch(self, remote_path: str, local_path: Path) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class RecordParser(Protocol):
    """把本地 AVL 解析成逻辑记录元组。"""

    stats: ParseStats

    def parse(self, path: Path) -> Iterator[Row]: ...


@runtime_checkable
class TableLayout(Protocol):
    """库表布局：单条记录路由 + 全部库表枚举（建表 / 过期清理用）。"""

    def all_targets(self) -> Iterator[tuple[Path, str]]: ...

    def target(self, filename: str, row: Row) -> tuple[Path, str]: ...


@runtime_checkable
class SinkStats(Protocol):
    """清理结果的可观测信号。"""

    errors: list[tuple[str, str]]


@runtime_checkable
class RecordSink(Protocol):
    """记录落地：建表、按布局分批写、过期清理。"""

    stats: SinkStats

    def ensure_schema(self) -> None: ...

    def write(self, filename: str, rows: Iterable[Row],
              layout: TableLayout | None = None) -> int: ...

    def purge_expired(self, cutoff: str,
                      tables: Iterable[str] | None = None) -> int: ...


@runtime_checkable
class StateStore(Protocol):
    """已成功入库的远程文件名清单。"""

    def has(self, remote_name: str) -> bool: ...

    def add(self, remote_name: str) -> None: ...


@dataclass(frozen=True)
class ImportResult:
    """单个远程文件的单轮结果。"""

    filename: str
    status: Literal["skip", "missing", "failed", "ok"]
    rows: int = 0
    skipped: int = 0
    detail: str = ""


__all__ = [
    "Row", "ParseStats", "ImportStatus", "ImportResult", "SinkStats",
    "RemoteSource", "RecordParser", "TableLayout", "RecordSink", "StateStore",
]
