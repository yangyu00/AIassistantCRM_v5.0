# -*- coding: utf-8 -*-
"""SQLite 表结构与 SQL 文本生成。

三条 SQL 的拼法约定见 ``README.md``；主键 / 时间列做成字段，方便其他项目替换。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class TableSchema:
    """一张业务表的列定义与 SQL 生成。"""

    columns: tuple[tuple[str, str], ...]
    primary_key: str = "product_no"
    time_column: str = "op_time"

    @classmethod
    def from_profile(cls, profile) -> "TableSchema":
        """从 ``TaskProfileConfig``（或任何有 ``.schema`` 的对象）构造。"""
        return cls(columns=tuple(tuple(c) for c in profile.schema))

    @property
    def column_names(self) -> list[str]:
        """列名顺序（不含类型）。"""
        return [c[0] for c in self.columns]

    def column_ddl(self) -> str:
        """列定义文本：``列名 类型`` 逗号连接。"""
        return ", ".join(f"{c} {t}" for c, t in self.columns)

    def create_table_sql(self, table_name: str) -> str:
        return (f"CREATE TABLE IF NOT EXISTS {table_name} "
                f"({self.column_ddl()}, PRIMARY KEY ({self.primary_key}))")

    def insert_sql(self, table_name: str) -> str:
        names = ", ".join(self.column_names)
        placeholders = ", ".join("?" for _ in self.column_names)
        return (f"INSERT OR REPLACE INTO {table_name} ({names}) "
                f"VALUES ({placeholders})")

    def delete_expired_sql(self, table_name: str) -> str:
        return f"DELETE FROM {table_name} WHERE {self.time_column} < ?"

    def validate_row(self, row: Iterable) -> None:
        """写库前校验列数，避免 SQLite 报出难懂的绑定错误。"""
        n = len(tuple(row))
        if n != len(self.columns):
            raise ValueError(
                f"记录列数 {n} 与表结构 {len(self.columns)} 列不一致"
            )


__all__ = ["TableSchema"]
