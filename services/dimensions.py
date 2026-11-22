# -*- coding: utf-8 -*-
"""在线侧卖点维表只读（``services`` 读取侧之一）。

与 ``services.reader``（业务库只读）并列：本模块只读磁盘上的
``dimensions.db``，按 ``product_id`` 批量取 ``selling_points`` 表，供两处在线
业务消费：

- **商机回捞**：``label_id``（「标签ID」）切成标签 ID 元组，拼辅助画像；
- **诊断新线兜底**：``product_name``（产品名称）、``fallback_script``
  （「兜底话术」原文）与 ``tag_*``（「标签N」原文，作回退），
  见 ``services/diagnosis/fallback.py``。诊断链路不查 IOP。

库里所有话术 / 标签列都只存原始文本，拆分与解析一律在读取侧完成。

**库路径、表名、列名由 ``services.db_config`` 独立声明**，不 import 后台写入侧
（``local_excel2db``）的任何配置或 registry：写入侧建表用的字段在这里冗余再写
一遍，读在线侧代码时一眼就能看清 SELECT 了哪些列。字段声明重复是本次解耦
刻意接受的成本。只认 ``db_config`` 白名单里的列，值一律参数绑定；只读连接
``mode=ro``。

唯一入口 ``load_selling_points(product_ids)``：一次请求调一次（批量 IN），
返回 ``{product_id: SellingPointRow}``。写入侧按固定间隔整表覆盖，所以**不做
进程级缓存**，避免新维表不生效；同一请求内靠返回的 dict 复用，多个相同 ID
只查一次。

库 / 表 / 必需列缺失或任何查询异常 → 降级返回 ``{}``，不抛给在线接口；
此时商机对象的标签子集为空、辅助画像为空，诊断兜底也无维表来源（维持原有
code=2 降级），其余话术段照常输出。

离线自检可用 ``configure(db_path)`` 把查询隔离到临时库（与
``reader.configure()`` 同风格），注入只改库文件位置，不改列映射。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from services import db_config

# 「标签ID」单元格是多个 ID，样例用换行分隔；兼容竖线 / 逗号 / 分号与全角变体。
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
    """一行卖点维表：只保留在线链路真正消费的列。

    ``label_ids`` 已按注册列「标签ID」拆好、去空、保序去重；顺序即辅助画像的
    拼接顺序。``fallback_script`` / ``tag_texts`` / ``match_result`` 都是库里的
    原始文本，不做结构化拆解；「兜底话术」为空时按 ``tag_0 … tag_N`` 顺序回退。
    """

    product_id: str
    product_name: str
    selling_point: str
    label_ids: tuple[str, ...]
    fallback_script: str = ""
    match_result: str = ""
    tag_texts: tuple[str, ...] = ()


def split_label_ids(raw, *, unique: bool = True) -> tuple[str, ...]:
    """把标签 ID 单元格拆成 ID 元组：去空白、去空项、保序。

    ``unique=True``（默认）用于商机画像的标签子集；``unique=False`` 保留重复项，
    供需要按原顺序使用原始文本的场景。
    """
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
    """替换读取侧的库路径（仅离线自检 / 单测注入用）。

    传 ``None`` 还原为 ``db_config`` 声明的默认库路径。表名与列契约是本模块
    的静态声明，注入只改库文件位置，不改列映射。
    """
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


def load_selling_points(product_ids) -> dict[str, SellingPointRow]:
    """按 product_id 批量取卖点维表行；任何失败降级 ``{}``。

    返回只包含**维表里真实命中**的 ID；未命中的 product_id 不在返回 dict 中，
    调用方据此得到空标签子集。库不存在 / 表缺失 / 列漂移 / 查询异常都会打日志
    并返回 ``{}``，不会让 API 500。
    """
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
           "load_selling_points"]
