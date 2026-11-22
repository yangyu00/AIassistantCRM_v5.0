# -*- coding: utf-8 -*-
"""registry.json 的对象模型：已校验、不可变、纯数据（零 I/O）。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ColumnSpec:
    """一条 Excel 表头 → SQLite 列的映射。"""

    source: str | None
    column_index: int | None
    column: str
    type: str                    # TEXT / INTEGER / REAL
    default: str = ""
    strip: bool = True
    optional: bool = False


@dataclass(frozen=True)
class TableSpec:
    """一张注册维表：来源文件、抽列定义、主键与动态列。"""

    table: str
    source: Path
    sheet: int | str
    header_row: int
    primary_key: tuple[str, ...]
    columns: tuple[ColumnSpec, ...]
    dynamic_columns: tuple[str, ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.column for c in self.columns)

    def column(self, name: str) -> ColumnSpec:
        for c in self.columns:
            if c.column == name:
                return c
        raise KeyError(name)


@dataclass(frozen=True)
class Registry:
    """写入侧唯一配置对象（冻结）。"""

    config_path: Path
    input_dir: Path
    runtime_dir: Path
    database: Path
    state_dir: Path
    lock_path: Path
    log_dir: Path
    poll_interval_seconds: int
    version_column: str
    meta_table: str
    tables: tuple[TableSpec, ...]

    def table(self, name: str) -> TableSpec | None:
        for t in self.tables:
            if t.table == name:
                return t
        return None


__all__ = ["ColumnSpec", "TableSpec", "Registry"]
