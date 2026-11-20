# -*- coding: utf-8 -*-
"""库表布局：既是「单条记录路由」，也是「全部库表枚举」，写入与建表同源。

三种 ``mode`` 的含义与分表口径见 ``Readme.md``「库表形式」一节。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..dbspec import PRODUCT_NO_INDEX, QUERY_MODES, QueryMode, shard_index


class UnknownPrefixError(ValueError):
    """文件名无法匹配任何已注册前缀。

    编排器按失败处理：不登记清单、本地文件保留、下轮重试。
    """


class PerPrefixLayout:
    """多库单表：文件名前缀 → ``{前缀}.db`` / ``data_{前缀}``。"""

    def __init__(self, db_dir: Path, prefixes):
        self.db_dir = Path(db_dir)
        self.prefixes = tuple(prefixes)
        if not self.prefixes:
            raise ValueError("多库单表布局至少需要 1 个文件前缀")

    def all_targets(self) -> Iterator[tuple[Path, str]]:
        for p in self.prefixes:
            yield self.db_dir / f"{p}.db", f"data_{p}"

    def target(self, filename: str, row: tuple) -> tuple[Path, str]:
        prefix = next((p for p in self.prefixes if filename.startswith(p)), None)
        if prefix is None:
            raise UnknownPrefixError(f"无法从文件名识别前缀: {filename}")
        return self.db_dir / f"{prefix}.db", f"data_{prefix}"


class Crc32ShardLayout:
    """单库多表：``crc32(product_no) % NUM_TABLES`` 分表（口径来自 dbspec）。"""

    def __init__(self, db_dir: Path, prefixes, num_tables: int,
                 table_name_prefix: str,
                 product_no_index: int = PRODUCT_NO_INDEX):
        self.db_dir = Path(db_dir)
        prefixes = tuple(prefixes)
        if not prefixes:
            raise ValueError("单库多表布局至少需要 1 个文件前缀")
        if num_tables <= 0:
            raise ValueError(f"NUM_TABLES 必须为正整数，当前为 {num_tables}")
        if not table_name_prefix:
            raise ValueError("单库多表布局需要 TABLE_NAME_PREFIX")
        self.db_path = self.db_dir / f"{prefixes[0]}.db"
        self.num_tables = int(num_tables)
        self.table_name_prefix = table_name_prefix
        self.product_no_index = int(product_no_index)

    def _table_of(self, product_no) -> str:
        return (f"{self.table_name_prefix}_"
                f"{shard_index(product_no, self.num_tables)}")

    def all_targets(self) -> Iterator[tuple[Path, str]]:
        for i in range(self.num_tables):
            yield self.db_path, f"{self.table_name_prefix}_{i}"

    def target(self, filename: str, row: tuple) -> tuple[Path, str]:
        return self.db_path, self._table_of(row[self.product_no_index])


class FixedTableLayout:
    """单库单表：一张固定表（预留，当前无任务使用）。"""

    def __init__(self, db_dir: Path, prefixes, table_name: str):
        prefixes = tuple(prefixes)
        if not prefixes:
            raise ValueError("单库单表布局至少需要 1 个文件前缀")
        if not table_name:
            raise ValueError("单库单表布局需要显式指定 table_name")
        self.db_path = Path(db_dir) / f"{prefixes[0]}.db"
        self.table_name = table_name

    def all_targets(self) -> Iterator[tuple[Path, str]]:
        yield self.db_path, self.table_name

    def target(self, filename: str, row: tuple) -> tuple[Path, str]:
        return self.db_path, self.table_name


def build_layout(profile, *, table_name: str | None = None):
    """按 ``profile.mode`` 构造布局（与读取侧 ``_resolve()`` 同一组 QueryMode 值）。

    ``profile`` 用鸭子类型：需要 ``mode`` / ``db_dir`` / ``file_prefixes`` /
    ``num_tables`` / ``table_name_prefix`` / ``table_name``（单库单表）。
    显式传 ``table_name`` 会覆盖 ``profile.table_name``，便于单测替换固定表名。
    """
    resolved = (table_name if table_name is not None
                else getattr(profile, "table_name", None))
    mode = profile.mode

    if mode == QueryMode.MULTI_DB_SINGLE_TABLE:
        return PerPrefixLayout(profile.db_dir, profile.file_prefixes)
    if mode == QueryMode.SINGLE_DB_MULTI_TABLE:
        # 两种 schema 的 product_no 都是第 3 列，与 dbspec.PRODUCT_NO_INDEX 同源。
        return Crc32ShardLayout(
            profile.db_dir, profile.file_prefixes,
            profile.num_tables, profile.table_name_prefix,
            product_no_index=PRODUCT_NO_INDEX)
    if mode == QueryMode.SINGLE_DB_SINGLE_TABLE:
        return FixedTableLayout(profile.db_dir, profile.file_prefixes,
                                resolved or "")
    raise ValueError(f"不支持的库表形式 {mode}；仅支持 {list(QUERY_MODES)}")


__all__ = [
    "PerPrefixLayout", "Crc32ShardLayout", "FixedTableLayout",
    "UnknownPrefixError", "build_layout",
]
