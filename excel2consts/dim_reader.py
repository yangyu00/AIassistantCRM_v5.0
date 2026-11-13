# -*- coding: utf-8 -*-
"""维表 SQLite 读取侧（在线业务用）。

只读打开 SQLite，不创建库、不写库；库/表/列未就绪时返回空 dict，
让业务沿用原有降级路径（标签名保持上游给的名字）。

数据库路径、表名、key/value 列都来自 registry.json，不硬编码。
"""
import sqlite3

from .dim_config import load_registry

_WARNED = set()


def _warn_once(msg):
    if msg not in _WARNED:
        _WARNED.add(msg)
        print(f"WARNING:: {msg}", flush=True)


def load_lookup(table_name, cfg=None):
    """把 registry 中声明了 lookup 的维表读成 {key: value}。

    :param table_name: 注册表名，如 "label_name_mapping"
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
        _warn_once(f"dim_reader: 表 {table_name} 未声明 lookup（key/value 列）")
        return {}

    db = cfg["database"]
    if not db.is_file():
        _warn_once(f"dim_reader: 维表库不存在 {db}，{table_name} 暂用空映射")
        return {}

    key_col, val_col = spec["lookup"]["key"], spec["lookup"]["value"]
    try:
        # mode=ro：在线业务绝不创建/写入维表库
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f"SELECT {key_col}, {val_col} FROM {table_name}"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _warn_once(f"dim_reader: 读取 {table_name} 失败（{e}），暂用空映射")
        return {}

    return {str(k): v for k, v in rows}
