# -*- coding: utf-8 -*-
"""SQLite 落地层：表结构 SQL、库表路由、建表 / 分批写 / 过期清理。

    from sftp_avl2db.sink import build_layout, build_sink, TableSchema

    layout = build_layout(profile)          # 满足 TableRouter，且能枚举全部库表
    sink = build_sink(profile, layout=layout)
    sink.ensure_schema()
    n = sink.write("F0959120261110000000.AVL", parser.parse(path), router=layout)

``build_sink(profile)`` 不传 layout 时内部自动构造一个；需要让「建表」
与「写入路由」严格共用同一份定义时，显式传同一个 layout 更稳妥。
"""
from ..protocols import RecordSink, TableRouter
from .router import (
    ROUTING_KINDS,
    Crc32ShardLayout,
    FixedTableLayout,
    PerPrefixLayout,
    TableLayout,
    UnknownPrefixError,
    build_layout,
    build_router,
)
from .schema import TableSchema
from .sqlite_sink import DEFAULT_SYNCHRONOUS, SqliteSink, WriteStats, build_sink

__all__ = [
    "TableSchema",
    "TableLayout", "PerPrefixLayout", "Crc32ShardLayout", "FixedTableLayout",
    "UnknownPrefixError", "ROUTING_KINDS", "build_layout", "build_router",
    "SqliteSink", "WriteStats", "DEFAULT_SYNCHRONOUS", "build_sink",
    "RecordSink", "TableRouter",
]
