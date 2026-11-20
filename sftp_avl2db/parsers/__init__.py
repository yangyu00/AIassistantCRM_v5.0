# -*- coding: utf-8 -*-
"""AVL 解析层：本地文件 → 逻辑记录元组，纯解析、无副作用。

两个实现都满足 ``protocols.RecordParser``；行为保真清单见 ``Readme.md``。
"""
from .continuation import (
    DATE_RE,
    ContinuationParser,
    split_head,
    strip_eol,
)
from .flat import ENCODING_CANDIDATES, FlatColumnParser
from ..protocols import ParseStats, RecordParser

PARSER_KINDS = ("flat", "continuation")


def build_parser(profile) -> RecordParser:
    """按任务装配声明选解析器。

    用鸭子类型接收任何带 ``.parser`` / ``.columns`` 的对象：本仓库用
    ``TaskProfileConfig``，其他项目直接构造 ``FlatColumnParser(12)`` 也行，
    不需要先造一份完整 profile。
    """
    kind = profile.parser
    if kind == "flat":
        return FlatColumnParser(len(profile.columns))
    if kind == "continuation":
        return ContinuationParser()
    raise ValueError(f"不支持的 parser 类型 {kind}；仅支持 {PARSER_KINDS}")


__all__ = [
    "FlatColumnParser",
    "ContinuationParser",
    "ParseStats",
    "RecordParser",
    "PARSER_KINDS",
    "ENCODING_CANDIDATES",
    "DATE_RE",
    "strip_eol",
    "split_head",
    "build_parser",
]
