"""库表布局：一个 AVL 一个 db，既是「单条记录路由」也是「全部库表枚举」。"""
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
        """枚举该 AVL 的全部 (库文件, 表名)"""
        if self.num_tables:
            for i in range(self.num_tables):
                yield self.db_path, make_table_name(self.base, i)
        else:
            yield self.db_path, make_table_name(self.base)

    def target(self, filename: str, row: tuple) -> tuple[Path, str]:
        """单条记录路由。"""
        if self.num_tables:
            idx = shard_index(row[self.product_no_index], self.num_tables)
            return self.db_path, make_table_name(self.base, idx)
        return self.db_path, make_table_name(self.base)


def build_layout(profile, *, table_name: str | None = None):
    """按 profile 装配布局：一个 AVL 一个 db。"""
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
