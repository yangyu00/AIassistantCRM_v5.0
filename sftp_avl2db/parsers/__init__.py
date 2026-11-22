# -*- coding: utf-8 -*-
"""AVL 解析层：本地文件 → 逻辑记录元组，纯解析、无副作用。"""
from .continuation import (
    DATE_RE,
    ContinuationParser,
    split_head,
    strip_eol,
)
from .flat import ENCODING, FlatColumnParser
from ..protocols import ParseStats, RecordParser

PARSER_KINDS = ("flat", "continuation")


def build_parser(profile) -> RecordParser:
    """按任务装配声明选解析器。"""
    kind = profile.parser
    if kind == "flat":
        columns = list(profile.columns)
        pk = getattr(profile, "primary_key", ())
        indexes = tuple(columns.index(col) for col in pk if col in columns)
        return FlatColumnParser(len(columns), primary_key_indexes=indexes)
    if kind == "continuation":
        return ContinuationParser()
    raise ValueError(f"不支持的 parser 类型 {kind}；仅支持 {PARSER_KINDS}")


__all__ = [
    "FlatColumnParser",
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
