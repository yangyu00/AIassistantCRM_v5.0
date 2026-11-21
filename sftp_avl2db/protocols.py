# -*- coding: utf-8 -*-
"""组件契约（纯 typing，零 I/O）：五层组件的 Protocol 与结果数据类。

任何一层都能换成测试替身；契约清单见 ``README.md``。
"""
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
    """单文件解析的统计信号。

    解析层不写日志，把计数留在这里，编排器按需记录：``rows`` 是产出的逻辑记录数
    （日志文本「共 N 条」），``skipped`` 是空主键跳过数（「跳过空主键 N 条」）。
    其余字段是各解析器特有的诊断信号，便于离线断言，不影响产出记录。
    """

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
    """远程（或伪远程）文件源。

    ``connect()`` 由编排器每轮显式调用；离线源实现为 no-op。
    """

    def connect(self) -> None: ...

    def exists(self, remote_path: str) -> bool: ...

    def fetch(self, remote_path: str, local_path: Path) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class RecordParser(Protocol):
    """把本地 AVL 解析成逻辑记录元组。

    ``parse()`` 必须是**生成器**：控损单文件约 7000 万逻辑记录，
    不能一次性装进 list。迭代过程中更新 ``stats``。
    """

    stats: ParseStats

    def parse(self, path: Path) -> Iterator[Row]: ...


@runtime_checkable
class TableLayout(Protocol):
    """库表布局：单条记录路由 + 全部库表枚举（建表 / 过期清理用）。"""

    def all_targets(self) -> Iterator[tuple[Path, str]]: ...

    def target(self, filename: str, row: Row) -> tuple[Path, str]: ...


@runtime_checkable
class SinkStats(Protocol):
    """清理结果的可观测信号；编排器只依赖 ``errors``。"""

    errors: list[tuple[str, str]]


@runtime_checkable
class RecordSink(Protocol):
    """记录落地：建表、按布局分批写、过期清理。

    ``stats.errors`` 供 ``runner.cleanup_expired()`` 汇总清理失败，因此任何
    替换实现都要带这个属性（收敛到 ``SinkStats`` 契约）。
    """

    stats: SinkStats

    def ensure_schema(self) -> None: ...

    def write(self, filename: str, rows: Iterable[Row],
              layout: TableLayout | None = None) -> int: ...

    def purge_expired(self, cutoff: str) -> int: ...


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
