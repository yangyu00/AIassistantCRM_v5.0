# -*- coding: utf-8 -*-
"""库表布局：一个 AVL 一个 db，既是「单条记录路由」也是「全部库表枚举」。

``{db_dir}/{avl}.db`` 是该 AVL 唯一的库文件；库内表名**恒以 AVL 名为基准**：
固定表就叫 ``{avl}``，分表叫 ``{avl}_{分表号}``（分表号口径来自 avl_specs）。
表名由 ``table_name()`` 统一派生，读写两侧共用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..avl_specs import (
    PRODUCT_NO_INDEX, shard_index, table_name as make_table_name,
)


class AvlDbLayout:
    """单个 AVL 的库表布局：一个 db 文件 + 固定表或分表。"""

    def __init__(self, db_dir: Path, avl: str, *, num_tables: int = 0,
                 table_name: str | None = None,
                 product_no_index: int = PRODUCT_NO_INDEX):
        self.db_path = Path(db_dir) / f"{avl}.db"
        # 默认表名 = AVL 名；table_name 仅是单测用的显式覆盖点。
        self.base = table_name or avl
        self.num_tables = int(num_tables)
        self.product_no_index = int(product_no_index)

        if self.num_tables < 0:
            raise ValueError(f"NUM_TABLES 不能为负，当前为 {self.num_tables}")
        if not self.base:
            raise ValueError("表名基准不能为空")

    def all_targets(self) -> Iterator[tuple[Path, str]]:
        """枚举该 AVL 的全部 (库文件, 表名)；建表与过期清理共用。"""
        if self.num_tables:
            for i in range(self.num_tables):
                yield self.db_path, make_table_name(self.base, i)
        else:
            yield self.db_path, make_table_name(self.base)

    def target(self, filename: str, row: tuple) -> tuple[Path, str]:
        """单条记录路由；不分表时全部落固定表，分表时按 product_no 取表。"""
        if self.num_tables:
            idx = shard_index(row[self.product_no_index], self.num_tables)
            return self.db_path, make_table_name(self.base, idx)
        return self.db_path, make_table_name(self.base)


def build_layout(profile, *, table_name: str | None = None):
    """按 profile 装配布局：一个 AVL 一个 db。

    分表键恒为 ``product_no``，列下标从注册的表结构推导（不硬编码第 3 列），
    这样新增分表 AVL 或调整列顺序都不会让写入侧悄悄写错分表。
    显式传 ``table_name`` 会覆盖默认的 AVL 名表名（分表时覆盖前缀），便于单测。
    """
    columns = [c[0] for c in profile.schema]
    if getattr(profile, "sharded", False):
        if "product_no" not in columns:
            raise ValueError(
                f"{profile.name}: 分表 AVL 的表结构缺少 product_no 列")
        product_no_index = columns.index("product_no")
    else:
        product_no_index = PRODUCT_NO_INDEX
    return AvlDbLayout(
        profile.db_dir, profile.name,
        num_tables=getattr(profile, "num_tables", 0),
        table_name=table_name,
        product_no_index=product_no_index,
    )


__all__ = ["AvlDbLayout", "build_layout"]
