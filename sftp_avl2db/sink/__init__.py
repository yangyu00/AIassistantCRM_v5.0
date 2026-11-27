"""SQLite 落地层：表结构 SQL、库表布局、建表 / 分批写 / 过期清理。

INSERT SQL 文本只在 ``schema.TableSchema.insert_sql()`` 生成；本次写入用哪种
模式由 ``SqliteSink`` 按「表能力 + 是否全量替换」单点推导。
"""
from .router import AvlDbLayout, build_layout
from .schema import (
    AVL_META_TABLE, INSERT_MODES, InsertMode, TableSchema,
    create_meta_table_sql, upsert_meta_sql,
)
from .sqlite_sink import DbHealth, SqliteSink, WriteStats, build_sink

__all__ = [
    "TableSchema", "AVL_META_TABLE", "create_meta_table_sql", "upsert_meta_sql",
    "InsertMode", "INSERT_MODES",
    "AvlDbLayout", "build_layout",
    "SqliteSink", "WriteStats", "DbHealth", "build_sink",
]
