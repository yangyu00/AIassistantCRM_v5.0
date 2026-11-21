# -*- coding: utf-8 -*-
"""AVL 解析层：本地文件 → 逻辑记录元组，纯解析、无副作用。

两个实现都满足 ``protocols.RecordParser``；解析行为见 ``README.md``。
``flat`` 覆盖商机回捞与诊断新线全部维表，差异（列数、主键列）全部由
``profile`` 的注册信息驱动。
"""
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
    """按任务装配声明选解析器。

    用鸭子类型接收任何带 ``.parser`` / ``.columns`` / ``.primary_key`` 的对象：
    本仓库用 ``TaskProfileConfig``，其他项目直接构造
    ``FlatColumnParser(12, primary_key_indexes=(2,))`` 也行，不需要先造一份
    完整 profile。

    ``flat`` 从 ``profile.primary_key`` 换算主键列下标传给解析器，让空主键
    跳过判定与建表 DDL 用的是同一份注册主键。商机与诊断维表共用这一个实现，
    末列不做任何类型转换（``city_id`` 业务上不消费，原样保留即可）。
    """
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
