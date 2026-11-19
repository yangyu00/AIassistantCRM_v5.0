# -*- coding: utf-8 -*-
"""registry.json 的对象模型：已校验、不可变、纯数据（零 I/O）。

由 ``RegistryLoader`` 构造；本模块不解析 JSON、不校验、不碰文件系统。
路径字段均为绝对 Path，相对路径已在加载时相对 registry 文件所在目录解析完毕。
"""
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
class LookupSpec:
    """读取侧映射声明：value 单列，或 values 多列（二者互斥）。"""

    key: str
    value: str | None = None
    values: tuple[str, ...] | None = None

    @property
    def is_multi(self) -> bool:
        return self.values is not None

    @property
    def select_columns(self) -> tuple[str, ...]:
        if self.values is not None:
            return (self.key,) + self.values
        assert self.value is not None, "lookup.value 缺失（应由 RegistryLoader 拦截）"
        return (self.key, self.value)


@dataclass(frozen=True)
class CutInScriptSpec:
    """读取侧重建话术 dict 所需的原始列角色；库里不存加工结果。"""

    key: str
    fallback: str
    groups: str
    tags: tuple[str, ...]
    fallback_key: str = "兜底话术"

    @property
    def select_columns(self) -> tuple[str, ...]:
        return (self.key, self.fallback, self.groups) + self.tags


@dataclass(frozen=True)
class TableSpec:
    """一张注册维表：来源、抽列定义、主键、以及可选的读取侧声明。"""

    table: str
    source: Path
    sheet: int | str
    header_row: int
    primary_key: tuple[str, ...]
    columns: tuple[ColumnSpec, ...]
    dynamic_columns: tuple[str, ...] = ()
    lookup: LookupSpec | None = None
    cut_in_script: CutInScriptSpec | None = None

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
    """一份已校验的注册配置；base_dir 为所有相对路径的解析基准。"""

    config_path: Path | None
    base_dir: Path
    database: Path
    poll_interval_seconds: int
    version_column: str
    meta_table: str
    tables: tuple[TableSpec, ...]

    def table(self, name: str) -> TableSpec | None:
        for t in self.tables:
            if t.table == name:
                return t
        return None


__all__ = ["ColumnSpec", "LookupSpec", "CutInScriptSpec", "TableSpec", "Registry"]
