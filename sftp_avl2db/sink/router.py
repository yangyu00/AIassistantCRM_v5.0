# -*- coding: utf-8 -*-
"""库表布局（``TableLayout``）：既是「单条记录路由」，也是「全部库表枚举」。

一个布局对象同时满足 ``sftp_avl2db.protocols.TableLayout`` 的
``target()`` 与 ``all_targets()``，以及 ``SqliteSink`` 建表 / 过期清理的需要。
这样「写入用哪张表」与「要建哪些表」只有一份定义，不会漂移。

``profile.mode`` 取值来自 ``dbspec.QUERY_SPECS[...]["mode"]``，与在线读取侧
``services/data_query.py::_resolve()`` 的分支值相同：

| mode | 库文件 | 表名 |
|---|---|---|
| ``multi_db_single_table`` | `{前缀}.db` 每前缀一个 | `data_{前缀}` |
| ``single_db_multi_table`` | `{FILE_PREFIXES[0]}.db` 单库 | `{前缀}_{crc32(product_no) % NUM_TABLES}` |
| ``single_db_single_table``（预留，当前无任务使用） | `{FILE_PREFIXES[0]}.db` 单库 | 显式指定的固定表名 |

分表口径与在线读取侧 ``services/data_query.py::_resolve()`` 完全一致：
``zlib.crc32(p_no.encode("utf-8")) % NUM_TABLES``，且 ``p_no`` 为空时取
字符串 ``"NULL"``（解析器已跳过空主键，属不可达分支，但口径保留）。
"""
from __future__ import annotations

import zlib
from pathlib import Path
from typing import Iterator


class UnknownPrefixError(ValueError):
    """文件名无法匹配任何已注册前缀。

    编排器按失败处理：不登记清单、本地文件保留、下轮重试。
    """


class _TableLayoutBase:
    """三个布局实现的内部基类（对外契约是 ``protocols.TableLayout``）。"""

    def all_targets(self) -> Iterator[tuple[Path, str]]:
        """枚举全部 ``(库文件路径, 表名)``，供建表与过期清理使用。"""
        raise NotImplementedError

    def target(self, filename: str, row: tuple) -> tuple[Path, str]:
        """一条记录该写到哪个库文件的哪张表。"""
        raise NotImplementedError


class PerPrefixLayout(_TableLayoutBase):
    """多库单表：文件名前缀 → ``{前缀}.db`` / ``data_{前缀}``。"""

    def __init__(self, db_dir: Path, prefixes):
        self.db_dir = Path(db_dir)
        self.prefixes = tuple(prefixes)
        if not self.prefixes:
            raise ValueError("多库单表布局至少需要 1 个文件前缀")

    def all_targets(self):
        for p in self.prefixes:
            yield self.db_dir / f"{p}.db", f"data_{p}"

    def target(self, filename: str, row: tuple):
        prefix = next((p for p in self.prefixes if filename.startswith(p)), None)
        if prefix is None:
            raise UnknownPrefixError(f"无法从文件名识别前缀: {filename}")
        return self.db_dir / f"{prefix}.db", f"data_{prefix}"


class Crc32ShardLayout(_TableLayoutBase):
    """单库多表：``crc32(product_no) % NUM_TABLES`` 分表。"""

    def __init__(self, db_dir: Path, prefixes, num_tables: int,
                 table_name_prefix: str, product_no_index: int = 2):
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
        p_no = product_no if product_no else "NULL"
        idx = zlib.crc32(str(p_no).encode("utf-8")) % self.num_tables
        return f"{self.table_name_prefix}_{idx}"

    def all_targets(self):
        for i in range(self.num_tables):
            yield self.db_path, f"{self.table_name_prefix}_{i}"

    def target(self, filename: str, row: tuple):
        return self.db_path, self._table_of(row[self.product_no_index])


class FixedTableLayout(_TableLayoutBase):
    """单库单表：一张固定表（预留，当前无任务使用）。"""

    def __init__(self, db_dir: Path, prefixes, table_name: str):
        prefixes = tuple(prefixes)
        if not prefixes:
            raise ValueError("单库单表布局至少需要 1 个文件前缀")
        if not table_name:
            raise ValueError("单库单表布局需要显式指定 table_name")
        self.db_path = Path(db_dir) / f"{prefixes[0]}.db"
        self.table_name = table_name

    def all_targets(self):
        yield self.db_path, self.table_name

    def target(self, filename: str, row: tuple):
        return self.db_path, self.table_name


def _build_multi_db_single_table(profile, table_name):
    return PerPrefixLayout(profile.db_dir, profile.file_prefixes)


def _build_single_db_multi_table(profile, table_name):
    idx = (list(profile.columns).index("product_no")
           if hasattr(profile, "columns") else 2)
    return Crc32ShardLayout(profile.db_dir, profile.file_prefixes,
                            profile.num_tables, profile.table_name_prefix,
                            product_no_index=idx)


def _build_single_db_single_table(profile, table_name):
    return FixedTableLayout(profile.db_dir, profile.file_prefixes,
                            table_name or "")


# 唯一的 mode → 布局分派表；键与 dbspec.QUERY_MODES 同值，不另立一套枚举。
_LAYOUT_BUILDERS = {
    "multi_db_single_table": _build_multi_db_single_table,
    "single_db_multi_table": _build_single_db_multi_table,
    "single_db_single_table": _build_single_db_single_table,
}


def build_layout(profile, *, table_name: str | None = None):
    """按 ``profile.mode`` 构造布局（与读取侧 ``_resolve()`` 同一组分支值）。

    ``profile`` 用鸭子类型：只需 ``mode`` / ``db_dir`` / ``file_prefixes``
    / ``num_tables`` / ``table_name_prefix`` / ``columns``（可取）。
    ``single_db_single_table`` 需要额外传 ``table_name``（不硬依赖 ``dbspec``，
    保证 ``sftp_avl2db`` 包单独 copy 出去也能用）。
    """
    builder = _LAYOUT_BUILDERS.get(profile.mode)
    if builder is None:
        raise ValueError(
            f"不支持的库表形式 {profile.mode}；仅支持 {list(_LAYOUT_BUILDERS)}")
    return builder(profile, table_name)


__all__ = [
    "PerPrefixLayout", "Crc32ShardLayout", "FixedTableLayout",
    "UnknownPrefixError", "build_layout",
]
