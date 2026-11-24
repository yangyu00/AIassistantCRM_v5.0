"""在线侧卖点维表只读（``services`` 读取侧之一）。"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from services import db_config

# 「标签ID」单元格是多个 ID，样例用换行分隔；支持竖线 / 逗号 / 分号与全角变体。
_SPLIT_LABEL_IDS_RE = re.compile(r"[\n\r|｜,，;；]+")

_CHUNK = 500                      # SQLite 绑定参数上限的保守分块
DEFAULT_DIMENSIONS_DB = db_config.default_dimensions_db()
_DB_PATH = None                   # None → db_config 声明的默认库路径


def _warn(msg):
    print(f"WARNING:: {msg}", flush=True)


def _error(msg):
    print(f"ERROR:: {msg}", flush=True)


@dataclass(frozen=True)
class SellingPointRow:
    """一行卖点维表：只保留在线链路真正消费的列。"""

    product_id: str
    product_name: str
    selling_point: str
    label_ids: tuple[str, ...]
    fallback_script: str = ""
    match_result: str = ""
    tag_texts: tuple[str, ...] = ()


def split_label_ids(raw, *, unique: bool = True) -> tuple[str, ...]:
    """把标签 ID 单元格拆成 ID 元组：去空白、去空项、保序。"""
    if raw is None:
        return ()
    parts = [p.strip() for p in _SPLIT_LABEL_IDS_RE.split(str(raw))]
    if not unique:
        return tuple(p for p in parts if p)
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)


def configure(db_path) -> None:
    """替换读取侧的库路径（仅离线自检 / 单测注入用）。"""
    global _DB_PATH
    _DB_PATH = Path(db_path) if db_path is not None else None


def _db_path() -> Path:
    return _DB_PATH if _DB_PATH is not None else DEFAULT_DIMENSIONS_DB


def _row_to_value(row) -> SellingPointRow:
    cols = db_config.SELLING_POINTS_SELECT_COLUMNS
    tag_cols = db_config.SELLING_POINTS_TAG_COLUMNS
    d = dict(zip(cols, row))
    return SellingPointRow(
        product_id=str(d.get("product_id") or "").strip(),
        product_name=str(d.get("product_name") or "").strip(),
        selling_point=str(d.get("selling_point") or ""),
        label_ids=split_label_ids(d.get("label_id")),
        fallback_script=str(d.get("fallback_script") or ""),
        match_result=str(d.get("match_result") or ""),
        tag_texts=tuple(str(d.get(c) or "") for c in tag_cols),
    )


def load_label_name_mapping() -> dict[str, str]:
    """读「标签转换维表」→ ``{label_id: 营销助手显示名字}``。

    ``local_excel2db`` 把 ``标签转换维表.xlsx`` 落成 ``label_name_mapping``
    表，在线侧按 ``db_config`` 声明只读两列。表缺失 / 列漂移 / 查询异常一律
    降级 ``{}``：此时辅助画像直接用 IOP 返回的 ``ITEMNAME``，输出结构不变。
    """
    db = _db_path()
    if not Path(db).is_file():
        _warn(f"标签名称映射库不存在: {db}")
        return {}
    try:
        cols = list(db_config.LABEL_NAME_MAPPING_COLUMNS)
        quoted = ", ".join(cols)
        uri = Path(db).resolve().as_uri() + "?mode=ro"
        out: dict[str, str] = {}
        conn = sqlite3.connect(uri, uri=True)
        try:
            sql = (f"SELECT {quoted} FROM "
                   f"{db_config.LABEL_NAME_MAPPING_TABLE}")
            for row in conn.execute(sql):
                d = dict(zip(cols, row))
                label_id = str(d.get("label_id") or "").strip()
                display_name = str(d.get("display_name") or "").strip()
                if label_id and display_name:
                    out[label_id] = display_name
        finally:
            conn.close()
        return out
    except Exception as e:
        _error(f"读取标签名称映射失败: {e}")
        return {}


def load_selling_points(product_ids) -> dict[str, SellingPointRow]:
    """按 product_id 批量取卖点维表行。"""
    # 去重保序：同一请求里重复的产品 ID 只查一次
    ids, seen = [], set()
    for pid in product_ids:
        s = str(pid).strip()
        if s and s not in seen:
            seen.add(s)
            ids.append(s)
    if not ids:
        return {}

    db = _db_path()
    if not Path(db).is_file():
        _warn(f"卖点维表库不存在: {db}")
        return {}

    try:
        cols = list(db_config.SELLING_POINTS_SELECT_COLUMNS)
        quoted = ", ".join(cols)
        uri = Path(db).resolve().as_uri() + "?mode=ro"
        out: dict[str, SellingPointRow] = {}
        conn = sqlite3.connect(uri, uri=True)
        try:
            # 分块避免单次绑定参数过多
            for start in range(0, len(ids), _CHUNK):
                chunk = ids[start:start + _CHUNK]
                placeholders = ", ".join("?" for _ in chunk)
                sql = (f"SELECT {quoted} FROM {db_config.SELLING_POINTS_TABLE} "
                       f"WHERE product_id IN ({placeholders})")
                for row in conn.execute(sql, tuple(chunk)):
                    value = _row_to_value(row)
                    if value.product_id:
                        out[value.product_id] = value
        finally:
            conn.close()
        return out
    except Exception as e:
        _error(f"读取卖点维表失败: {e}")
        return {}


__all__ = ["SellingPointRow", "split_label_ids", "configure",
           "load_selling_points", "load_label_name_mapping"]
