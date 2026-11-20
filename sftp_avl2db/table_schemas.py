# -*- coding: utf-8 -*-
"""表结构定义：按名字注册，供多个 AVL 复用。

一个 AVL 对应一个 db；表结构不再绑定「任务」，而是绑定这里的 ``schema`` 名。
同一业务的多个 AVL 可以引用同一个名字，未来新增业务里不同表结构的 AVL 只需
在这里加一份定义，再到 ``avl_specs.AVL_SPECS`` 里指过来。

纯静态、零 I/O；写入侧建表 DDL 与读取侧 ``SELECT *`` 组装 dict 共用列顺序。
"""
from __future__ import annotations

from typing import Final, Mapping

TABLE_SCHEMAS: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    "opportunity_recall": (
        ("op_time", "TEXT"),
        ("call_time", "TEXT"),
        ("product_no", "TEXT NOT NULL"),
        ("type_dl", "VARCHAR(50)"),
        ("type_xl", "VARCHAR(50)"),
        ("content_abstract", "TEXT"),
        ("cust_demand", "TEXT"),
        ("rec_strategy", "TEXT"),
        ("reject_reason", "TEXT"),
        ("reason_note", "TEXT"),
        ("refer_strategy", "TEXT"),
        ("city_id", "INTEGER"),
    ),
    "loss_control": (
        ("op_time", "TEXT"),
        ("user_id", "TEXT"),
        ("product_no", "TEXT NOT NULL"),
        ("city_id", "TEXT"),
        ("strategy_name", "TEXT"),
    ),
}


def schema_of(name: str) -> tuple[tuple[str, str], ...]:
    """按名字取整份列定义；未注册直接报错，不静默回落。"""
    try:
        return TABLE_SCHEMAS[name]
    except KeyError as e:
        raise KeyError(f"未注册的表结构 {name!r}；请在 table_schemas.py 中定义") from e


def schema_columns(name: str) -> list[str]:
    """列名顺序（不含类型），写入 DDL 与读取侧组装 dict 共用。"""
    return [c[0] for c in schema_of(name)]


__all__ = ["TABLE_SCHEMAS", "schema_of", "schema_columns"]
