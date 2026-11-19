# -*- coding: utf-8 -*-
"""``sftp_avl2db`` 的组件契约（纯 typing，零 I/O、零第三方依赖）。

这里一次把五层接口全部定好，后续阶段只新增「实现」，不再改本文件：

    RemoteSource   SFTP / 本地目录源文件        （阶段 4）
    RecordParser   AVL → 逻辑记录生成器          （阶段 2）
    TableRouter    记录 → (库文件, 表名)         （阶段 3）
    RecordSink     SQLite 建表 / 写入 / 清理     （阶段 3）
    StateStore     断点清单 / 单实例锁的状态读写  （阶段 4）

协议只约束方法形状，不约束实现技术；``Avl2DbRunner``（阶段 5）通过构造器
接收这些对象，因此任何一层都能换成测试替身或宿主项目自己的实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Protocol, runtime_checkable

Row = tuple


@dataclass
class ParseStats:
    """单文件解析的统计信号（替代原解析循环里的 ``log.info`` / ``log.warning``）。

    解析层不写日志，把这些本来就要打印的计数留在这里，编排器按需记录：
    ``rows`` 是产出的逻辑记录数（对应原日志「共 N 条」），
    ``skipped`` 是空主键跳过数（对应「跳过空手机号 N 条」）。
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
    """远程（或伪远程）文件源。"""

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
class TableRouter(Protocol):
    """一条记录该写到哪个库文件的哪张表。"""

    def target(self, filename: str, row: Row) -> tuple[Path, str]: ...


@runtime_checkable
class RecordSink(Protocol):
    """记录落地：建表、按路由分批写、过期清理。"""

    def ensure_schema(self) -> None: ...

    def write(self, filename: str, rows: Iterable[Row],
              router: TableRouter) -> int: ...

    def purge_expired(self, cutoff: str) -> int: ...


@runtime_checkable
class StateStore(Protocol):
    """已成功入库的远程文件名清单。"""

    def has(self, remote_name: str) -> bool: ...

    def add(self, remote_name: str) -> None: ...


@dataclass(frozen=True)
class ImportResult:
    """单个远程文件的单轮结果（阶段 5 编排器使用，先在此定义避免后续改文件）。"""

    filename: str
    status: str                  # "skip" | "missing" | "failed" | "ok"
    rows: int = 0
    skipped: int = 0
    detail: str = ""


__all__ = [
    "Row", "ParseStats", "ImportResult",
    "RemoteSource", "RecordParser", "TableRouter", "RecordSink", "StateStore",
]
