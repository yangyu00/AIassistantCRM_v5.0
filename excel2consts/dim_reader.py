# -*- coding: utf-8 -*-
"""维表 SQLite 读取侧（在线业务用）。

只读打开 SQLite，不创建库、不写库；库/表/列未就绪时返回空 dict，
让业务沿用原有降级路径。

数据库路径、表名、key/value 列都来自 registry.json，不硬编码。
lookup 支持两种形状（由 registry 声明）：
  value : 单列  -> {key: 单值}
  values: 多列  -> {key: (值1, 值2, ...)}  顺序即 values 声明顺序
"""
import sqlite3

from .dim_config import load_registry

_WARNED = set()


def _warn_once(msg):
    if msg not in _WARNED:
        _WARNED.add(msg)
        print(f"WARNING:: {msg}", flush=True)


def load_lookup(table_name, cfg=None):
    """读取注册表的 lookup 映射。

    :param table_name: 注册表名，如 "label_name_mapping" /
                       "product_selling_point_match"
    :return: dict；库不存在、表不存在、未声明 lookup 或查询失败时返回 {}
    """
    try:
        cfg = cfg or load_registry()
    except (OSError, ValueError) as e:
        _warn_once(f"dim_reader: registry 配置读取失败（{e}），{table_name} 暂用空映射")
        return {}

    spec = next((t for t in cfg["tables"] if t["table"] == table_name), None)
    if spec is None:
        _warn_once(f"dim_reader: 未注册的表 {table_name}")
        return {}
    if not spec.get("lookup"):
        _warn_once(f"dim_reader: 表 {table_name} 未声明 lookup")
        return {}

    db = cfg["database"]
    if not db.is_file():
        _warn_once(f"dim_reader: 维表库不存在 {db}，{table_name} 暂用空映射")
        return {}

    lk = spec["lookup"]
    key_col = lk["key"]
    value_cols = [lk["value"]] if lk["values"] is None else lk["values"]
    multi = lk["values"] is not None
    select_cols = ", ".join([key_col] + value_cols)

    try:
        # mode=ro：在线业务绝不创建/写入维表库
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f"SELECT {select_cols} FROM {table_name}"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _warn_once(f"dim_reader: 读取 {table_name} 失败（{e}），暂用空映射")
        return {}

    def clean(v):
        return "" if v is None else v

    if multi:
        return {str(row[0]): tuple(clean(v) for v in row[1:]) for row in rows}
    return {str(row[0]): clean(row[1]) for row in rows}


def load_product_selling_point(cfg=None):
    """卖点维表：{business_id: (product_name, selling_point, label_id)}。

    形状与旧 consts.product_selling_point_match_dict 完全一致，
    因此业务侧的 [0] / [1] / [2].split("\\n") 无需改动。
    """
    return load_lookup("product_selling_point_match", cfg=cfg)
