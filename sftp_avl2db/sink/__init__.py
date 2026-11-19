# -*- coding: utf-8 -*-
"""SQLite 落地层：表结构 SQL、库表布局、建表 / 分批写 / 过期清理。

    from sftp_avl2db.sink import build_layout, build_sink, TableSchema

    layout = build_layout(profile)          # 满足 TableLayout，且能枚举全部库表
    sink = build_sink(profile, layout=layout)
    sink.ensure_schema()
    n = sink.write("F0959120261110000000.AVL", parser.parse(path), layout=layout)

``build_sink(profile)`` 不传 layout 时内部自动构造一个；需要让「建表」
与「写入路由」严格共用同一份定义时，显式传同一个 layout 更稳妥。
"""
from ..protocols import RecordSink, TableLayout
from .router import (
    Crc32ShardLayout,
    FixedTableLayout,
    PerPrefixLayout,
    UnknownPrefixError,
    build_layout,
)
from .schema import TableSchema
from .sqlite_sink import DEFAULT_SYNCHRONOUS, SqliteSink, WriteStats, build_sink

__all__ = [
    "TableSchema",
    "TableLayout", "PerPrefixLayout", "Crc32ShardLayout", "FixedTableLayout",
    "UnknownPrefixError", "build_layout",
    "SqliteSink", "WriteStats", "DEFAULT_SYNCHRONOUS", "build_sink",
    "RecordSink",
]
