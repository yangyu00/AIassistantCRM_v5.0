# -*- coding: utf-8 -*-
"""库表布局（``TableLayout``）：既是「单条记录路由」，也是「全部库表枚举」。

一个布局对象同时满足 ``sftp_avl2db.protocols.TableRouter`` 的 ``target()``，
以及 ``SqliteSink`` 建表 / 过期清理需要的 ``all_targets()``。这样「写入用哪张表」
与「要建哪些表」只有一份定义，不会漂移。

三种形式与 ``crm_dbspec.QUERY_SPECS[...]["mode"]`` 一一对应：

| routing | 旧代码 | 库文件 | 表名 |
|---|---|---|---|
| ``per_prefix`` | `OpportunityRecallTask` | `{前缀}.db` 每前缀一个 | `data_{前缀}` |
| ``crc32_shards`` | `LossControlTask` | `{FILE_PREFIXES[0]}.db` 单库 | `{前缀}_{crc32(product_no) % NUM_TABLES}` |
| ``fixed_table`` | 预留（当前无任务使用） | `{FILE_PREFIXES[0]}.db` 单库 | 显式指定的固定表名 |

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

    ⚠️ **与旧代码的一处差异，留给阶段 5 编排器处理**：
    旧 ``OpportunityRecallTask.parse_and_save()`` 在识别不到前缀时是
    ``log.error(...)`` 后 ``return``（静默跳过）；但上层
    ``BaseSftpTask.run_once_for_date()`` 并不感知这个 return，仍会
    ``manifest.add()`` 并 ``imported += 1``——文件被标记为已入库却没写库。
    该分支在实际运行中不可达（上层只下发按 ``FILE_PREFIXES`` 拼出的文件名），
    阶段 3 选择抛异常让它显式可见，阶段 5 接线时再决定是复刻旧语义还是
    把「识别不到前缀」当作失败重试。此处不偷偷吞掉。
    """


class TableLayout:
    """库表布局基类。子类实现 ``all_targets()`` 与 ``target()``。"""

    def all_targets(self) -> Iterator[tuple[Path, str]]:
        """枚举全部 ``(库文件路径, 表名)``，供建表与过期清理使用。"""
        raise NotImplementedError

    def target(self, filename: str, row: tuple) -> tuple[Path, str]:
        """一条记录该写到哪个库文件的哪张表。"""
        raise NotImplementedError


class PerPrefixLayout(TableLayout):
    """多库单表：文件名前缀 → ``{前缀}.db`` / ``data_{前缀}``。"""

    def __init__(self, db_dir: Path, prefixes):
        self.db_dir = Path(db_dir)
        self.prefixes = tuple(prefixes)
        if not self.prefixes:
            raise ValueError("per_prefix 布局至少需要 1 个文件前缀")

    def all_targets(self):
        for p in self.prefixes:
            yield self.db_dir / f"{p}.db", f"data_{p}"

    def target(self, filename: str, row: tuple):
        prefix = next((p for p in self.prefixes if filename.startswith(p)), None)
        if prefix is None:
            raise UnknownPrefixError(f"无法从文件名识别前缀: {filename}")
        return self.db_dir / f"{prefix}.db", f"data_{prefix}"


class Crc32ShardLayout(TableLayout):
    """单库多表：``crc32(product_no) % NUM_TABLES`` 分表。"""

    def __init__(self, db_dir: Path, prefixes, num_tables: int,
                 table_name_prefix: str, product_no_index: int = 2):
        self.db_dir = Path(db_dir)
        prefixes = tuple(prefixes)
        if not prefixes:
            raise ValueError("crc32_shards 布局至少需要 1 个文件前缀")
        if num_tables <= 0:
            raise ValueError(f"NUM_TABLES 必须为正整数，当前为 {num_tables}")
        if not table_name_prefix:
            raise ValueError("crc32_shards 布局需要 TABLE_NAME_PREFIX")
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


class FixedTableLayout(TableLayout):
    """单库单表：一张固定表（预留，当前无任务使用）。"""

    def __init__(self, db_dir: Path, prefixes, table_name: str):
        prefixes = tuple(prefixes)
        if not prefixes:
            raise ValueError("fixed_table 布局至少需要 1 个文件前缀")
        if not table_name:
            raise ValueError("fixed_table 布局需要显式指定 table_name")
        self.db_path = Path(db_dir) / f"{prefixes[0]}.db"
        self.table_name = table_name

    def all_targets(self):
        yield self.db_path, self.table_name

    def target(self, filename: str, row: tuple):
        return self.db_path, self.table_name


ROUTING_KINDS = ("per_prefix", "crc32_shards", "fixed_table")


def build_layout(profile, *, table_name: str | None = None) -> TableLayout:
    """按 ``profile.routing`` 构造布局。

    ``profile`` 用鸭子类型：只需 ``routing`` / ``db_dir`` / ``file_prefixes``
    / ``num_tables`` / ``table_name_prefix`` / ``columns``（可取）。
    ``fixed_table`` 需要额外传 ``table_name``（不硬依赖 ``crm_dbspec``，
    保证 ``sftp_avl2db`` 包单独 copy 出去也能用）。
    """
    kind = profile.routing
    prefixes = profile.file_prefixes
    if kind == "per_prefix":
        return PerPrefixLayout(profile.db_dir, prefixes)
    if kind == "crc32_shards":
        idx = (list(profile.columns).index("product_no")
               if hasattr(profile, "columns") else 2)
        return Crc32ShardLayout(profile.db_dir, prefixes,
                                profile.num_tables, profile.table_name_prefix,
                                product_no_index=idx)
    if kind == "fixed_table":
        return FixedTableLayout(profile.db_dir, prefixes, table_name or "")
    raise ValueError(f"不支持的 routing 类型 {kind}；仅支持 {ROUTING_KINDS}")


# 方案文档里用的是 build_router 这个名字，给一个别名方便阶段 5 装配。
build_router = build_layout


__all__ = [
    "TableLayout", "PerPrefixLayout", "Crc32ShardLayout", "FixedTableLayout",
    "UnknownPrefixError", "ROUTING_KINDS", "build_layout", "build_router",
]
