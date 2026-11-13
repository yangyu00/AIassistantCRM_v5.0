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
from .dim_transform import build_cut_in_script_dict

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


def load_cut_in_script(cfg=None):
    """切入/挽留话术：{business_id: {标签ID / 兜底话术: [切入, 挽留] 或 ""}}。

    库里只存「兜底话术」「匹配结果2」「标签0…标签N」原始列，拆解加工由
    dim_transform 在读取侧完成；返回形状与旧 consts.cut_in_script_dict 完全一致，
    所以 rewrite_by_excel 里的 [0] / [1] 取值逻辑无需改动。

    列名、标签列顺序、兜底 key 全部取自 registry.json 的 cut_in_script 声明。
    """
    try:
        cfg = cfg or load_registry()
    except (OSError, ValueError) as e:
        _warn_once(f"dim_reader: registry 配置读取失败（{e}），cut_in_script 暂用空映射")
        return {}

    spec = next((t for t in cfg["tables"] if t["table"] == "product_selling_point_match"), None)
    if spec is None:
        _warn_once("dim_reader: 未注册的表 product_selling_point_match")
        return {}
    ci = spec.get("cut_in_script")
    if not ci:
        _warn_once("dim_reader: product_selling_point_match 未声明 cut_in_script")
        return {}

    db = cfg["database"]
    if not db.is_file():
        _warn_once(f"dim_reader: 维表库不存在 {db}，cut_in_script 暂用空映射")
        return {}

    select_cols = [ci["key"], ci["fallback"], ci["groups"]] + ci["tags"]
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f"SELECT {', '.join(select_cols)} "
                f"FROM {spec['table']} ORDER BY rowid"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        # 典型场景：旧库未重建，缺少 tag_* 列
        _warn_once(f"dim_reader: 读取 cut_in_script 失败（{e}），暂用空映射；"
                   "若提示 no such column，请删除维表库后由 dim_watcher 重建")
        return {}

    dicts = [dict(zip(select_cols, row)) for row in rows]
    return build_cut_in_script_dict(
        dicts,
        key_col=ci["key"], fallback_col=ci["fallback"], groups_col=ci["groups"],
        tag_cols=ci["tags"], fallback_key=ci["fallback_key"],
    )
