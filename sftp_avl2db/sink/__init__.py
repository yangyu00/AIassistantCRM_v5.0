# -*- coding: utf-8 -*-
"""SQLite 落地层：表结构 SQL、库表布局、建表 / 分批写 / 过期清理。

公共 API 只认包根 ``sftp_avl2db``；本子包只暴露实现类与工厂。
"""
from .router import AvlDbLayout, build_layout
from .schema import TableSchema
from .sqlite_sink import DEFAULT_SYNCHRONOUS, SqliteSink, WriteStats, build_sink

__all__ = [
    "TableSchema",
    "AvlDbLayout", "build_layout",
    "SqliteSink", "WriteStats", "DEFAULT_SYNCHRONOUS", "build_sink",
]
