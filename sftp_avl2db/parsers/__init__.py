# -*- coding: utf-8 -*-
"""AVL 解析层：本地文件 → 逻辑记录元组，纯解析、无副作用。

三个实现都满足 ``protocols.RecordParser``；解析行为见 ``README.md``。
"""
from .continuation import (
    DATE_RE,
    ContinuationParser,
    split_head,
    strip_eol,
)
from .dimension import DimensionFlatParser
from .flat import ENCODING, FlatColumnParser
from ..protocols import ParseStats, RecordParser

PARSER_KINDS = ("flat", "continuation", "dimension")


def build_parser(profile) -> RecordParser:
    """按任务装配声明选解析器。

    用鸭子类型接收任何带 ``.parser`` / ``.columns`` 的对象：本仓库用
    ``TaskProfileConfig``，其他项目直接构造 ``FlatColumnParser(12)`` 也行，
    不需要先造一份完整 profile。

    ``dimension`` 额外从 ``profile.primary_key`` 换算主键列下标传给解析器，
    让空主键跳过判定与建表 DDL 用的是同一份注册主键。
    """
    kind = profile.parser
    if kind == "flat":
        return FlatColumnParser(len(profile.columns))
    if kind == "continuation":
        return ContinuationParser()
    if kind == "dimension":
        columns = list(profile.columns)
        pk = getattr(profile, "primary_key", ())
        indexes = tuple(columns.index(col) for col in pk if col in columns)
        return DimensionFlatParser(len(columns), primary_key_indexes=indexes)
    raise ValueError(f"不支持的 parser 类型 {kind}；仅支持 {PARSER_KINDS}")


__all__ = [
    "FlatColumnParser",
    "DimensionFlatParser",
    "ContinuationParser",
    "ParseStats",
    "RecordParser",
    "PARSER_KINDS",
    "ENCODING",
    "DATE_RE",
    "strip_eol",
    "split_head",
    "build_parser",
]
