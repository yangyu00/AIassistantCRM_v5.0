"""SQLite 落地层：表结构 SQL、库表布局、建表 / 分批写 / 过期清理。"""
from .router import AvlDbLayout, build_layout
from .schema import (
    AVL_META_TABLE, TableSchema, create_meta_table_sql, upsert_meta_sql,
)
from .sqlite_sink import SqliteSink, WriteStats, build_sink

__all__ = [
    "TableSchema", "AVL_META_TABLE", "create_meta_table_sql", "upsert_meta_sql",
    "AvlDbLayout", "build_layout",
    "SqliteSink", "WriteStats", "build_sink",
]
