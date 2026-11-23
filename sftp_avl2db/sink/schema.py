# -*- coding: utf-8 -*-
"""SQLite 表结构与 SQL 文本生成。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable


# 每个 {AVL}.db 内的批次元数据表（写入侧维护，在线侧按同名只读）。
# 表名带下划线前缀，不与业务表 {AVL} / {AVL}_{分表号} 冲突；它不进
# layout.all_targets()，因此不参与分表路由、也不被 purge_expired() 清理。
AVL_META_TABLE: Final[str] = "_avl_meta"


def create_meta_table_sql() -> str:
    """批次元数据表：一个 AVL 一行，记录「最新成功入库的文件日期 / 数据日期」。"""
    return (
        f"CREATE TABLE IF NOT EXISTS {AVL_META_TABLE} ("
        "avl TEXT PRIMARY KEY, "
        "latest_file_date TEXT NOT NULL, "   # YYYYMMDD，来自文件名
        "latest_op_time TEXT NOT NULL, "     # 该批数据里最大的 op_time（原样文本）
        "last_filename TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )


def upsert_meta_sql() -> str:
    """只在新文件日期不早于已记录日期时推进，避免补跑旧文件把水位拉回去。"""
    return (
        f"INSERT INTO {AVL_META_TABLE} "
        "(avl, latest_file_date, latest_op_time, last_filename, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(avl) DO UPDATE SET "
        "latest_file_date = excluded.latest_file_date, "
        "latest_op_time = excluded.latest_op_time, "
        "last_filename = excluded.last_filename, "
        "updated_at = excluded.updated_at "
        f"WHERE excluded.latest_file_date >= {AVL_META_TABLE}.latest_file_date"
    )


@dataclass(frozen=True)
class TableSchema:
    """一张业务表的列定义与 SQL 生成。"""

    columns: tuple[tuple[str, str], ...]
    primary_key: str | tuple[str, ...] = "product_no"
    time_column: str = "op_time"

    @classmethod
    def from_profile(cls, profile) -> "TableSchema":
        """从 ``TaskProfileConfig``（或任何有 ``.schema`` 的对象）构造。"""
        return cls(
            columns=tuple(tuple(c) for c in profile.schema),
            primary_key=getattr(profile, "primary_key", "product_no"),
            time_column=getattr(profile, "time_column", "op_time"),
        )

    @property
    def primary_key_columns(self) -> tuple[str, ...]:
        """主键列统一成元组，单列写法与复合写法共用一条 SQL 生成路径。"""
        if isinstance(self.primary_key, str):
            return (self.primary_key,)
        return tuple(self.primary_key)

    @property
    def column_names(self) -> list[str]:
        """列名顺序（不含类型）。"""
        return [c[0] for c in self.columns]

    def column_ddl(self) -> str:
        """列定义文本：``列名 类型`` 逗号连接。"""
        pk = set(self.primary_key_columns)
        parts = []
        for name, decl in self.columns:
            if name in pk and "NOT NULL" not in decl.upper():
                decl = f"{decl} NOT NULL"
            parts.append(f"{name} {decl}")
        return ", ".join(parts)

    def create_table_sql(self, table_name: str) -> str:
        pk = ", ".join(self.primary_key_columns)
        return (f"CREATE TABLE IF NOT EXISTS {table_name} "
                f"({self.column_ddl()}, PRIMARY KEY ({pk}))")

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


__all__ = [
    "TableSchema", "AVL_META_TABLE", "create_meta_table_sql", "upsert_meta_sql",
]
