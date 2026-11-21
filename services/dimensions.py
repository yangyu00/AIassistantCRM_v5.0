# -*- coding: utf-8 -*-
"""在线侧卖点维表只读（``services`` 读取侧之一）。

与 ``services.reader``（业务库只读）并列：本模块只读 ``local_excel2db``
写入侧产出的 ``dimensions.db``，按 ``product_id`` 批量取 ``selling_points``
表，供两处在线业务消费：

- **商机回捞**：``label_id``（「标签ID」）切成标签 ID 元组，拼辅助画像；
- **诊断新线兜底**：``product_name``（产品名称）、``match_result``
  （「匹配结果2」的场景标签 ID 列表）与 ``tag_*``（「标签0…标签N」话术单元格），
  见 ``services/diagnosis/fallback.py``。

**契约与写入侧同源，不另写一套列名**：

- 库路径取 ``local_excel2db.config.DB_PATH``（纯常量，零 I/O）；
- 表名 / 列名 / 主键来自 ``local_excel2db.config.load_registry()`` 里
  ``selling_points`` 的 ``TableSpec``，即 ``registry.json`` 的声明；
- 只认 registry 白名单里的列，值一律参数绑定；只读连接 ``mode=ro``。

唯一入口 ``load_selling_points(product_ids)``：一次请求调一次（批量 IN），
返回 ``{product_id: SellingPointRow}``。写入侧按 ``poll_interval_seconds``
整表覆盖，所以**不做进程级缓存**，避免新维表不生效；同一请求内靠返回的
dict 复用，多个相同 ID 只查一次。

库 / 表 / 必需列缺失或任何查询异常 → 降级返回 ``{}``，不抛给在线接口；
此时商机对象的标签子集为空、辅助画像为空，诊断兜底也无维表来源（维持原有
code=2 降级），其余话术段照常输出。

离线自检可用 ``configure(db_path)`` 把查询隔离到临时库（与
``reader.configure()`` 同风格），不影响写入侧任何路径。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from local_excel2db.config import DB_PATH, load_registry

# registry 里 selling_points 的列名。必需列缺失直接降级，可选列缺失填空串。
_REQUIRED_COLUMNS = ("product_id", "label_id")
_OPTIONAL_COLUMNS = ("product_name", "selling_point", "fallback_script",
                     "match_result")

# 「标签ID」单元格是多个 ID，样例用换行分隔；兼容竖线 / 逗号 / 分号与全角变体。
_SPLIT_LABEL_IDS_RE = re.compile(r"[\n\r|｜,，;；]+")

_CHUNK = 500                      # SQLite 绑定参数上限的保守分块
_DB_PATH = None                   # None → local_excel2db.config.DB_PATH
_SPEC = None                      # 懒加载的 selling_points TableSpec，进程内缓存一次


def _warn(msg):
    print(f"WARNING:: {msg}", flush=True)


def _error(msg):
    print(f"ERROR:: {msg}", flush=True)


@dataclass(frozen=True)
class SellingPointRow:
    """一行卖点维表：只保留在线链路真正消费的列。

    ``label_ids`` 已按注册列「标签ID」拆好、去空、保序去重；顺序即辅助画像的
    拼接顺序。``tag_texts`` 按 ``tag_0 … tag_N`` 的下标顺序保留「标签N」单元格
    原文，与 ``scene_label_ids``（「匹配结果2」）**按下标一一对应**。
    """

    product_id: str
    product_name: str
    selling_point: str
    label_ids: tuple[str, ...]
    fallback_script: str = ""
    match_result: str = ""
    tag_texts: tuple[str, ...] = ()

    @property
    def scene_label_ids(self) -> tuple[str, ...]:
        """「匹配结果2」的场景标签 ID，按位置保留、**不去重**。

        与 ``tag_texts`` 同下标：第 i 个 ID 对应「标签i」单元格。这里不能去重，
        否则重复 ID 会让后续 tag 下标整体错位。
        """
        return split_label_ids(self.match_result, unique=False)


def split_label_ids(raw, *, unique: bool = True) -> tuple[str, ...]:
    """把标签 ID 单元格拆成 ID 元组：去空白、去空项、保序。

    ``unique=True``（默认）用于商机画像的标签子集；``unique=False`` 用于
    「匹配结果2」这种**按下标对应**另一个列表的场景，重复项必须保留。
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

    传 ``None`` 还原为 ``local_excel2db.config.DB_PATH``。表名与列契约仍来自
    ``registry.json``，注入只改库文件位置，不改列映射。
    """
    global _DB_PATH, _SPEC
    _DB_PATH = Path(db_path) if db_path is not None else None
    _SPEC = None


def _db_path() -> Path:
    return _DB_PATH if _DB_PATH is not None else DB_PATH


def _spec():
    """懒加载 selling_points 的 TableSpec；registry 改了重启在线进程生效。"""
    global _SPEC
    if _SPEC is None:
        spec = load_registry().table("selling_points")
        if spec is None:
            raise ValueError("registry.json 未注册 selling_points 表")
        names = set(spec.column_names)
        missing = [c for c in _REQUIRED_COLUMNS if c not in names]
        if missing:
            raise ValueError(
                f"selling_points 缺必需列 {missing}；请检查 registry.json")
        _SPEC = spec
    return _SPEC


def _select_columns(spec):
    """返回 ``(全部 SELECT 列, tag 动态列)``；列名全部来自 registry 白名单。

    ``tag_*`` 顺序取 ``TableSpec.dynamic_columns``（即 registry 的
    ``dynamic_columns`` 展开结果），与「匹配结果2」的下标一一对齐。
    """
    names = set(spec.column_names)
    cols = list(_REQUIRED_COLUMNS)
    cols += [c for c in _OPTIONAL_COLUMNS if c in names]
    tag_cols = tuple(c for c in spec.dynamic_columns if c in names)
    return cols + list(tag_cols), tag_cols


def _row_to_value(row, cols, tag_cols) -> SellingPointRow:
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
        spec = _spec()
        cols, tag_cols = _select_columns(spec)
        quoted = ", ".join(cols)
        uri = Path(db).resolve().as_uri() + "?mode=ro"
        out: dict[str, SellingPointRow] = {}
        conn = sqlite3.connect(uri, uri=True)
        try:
            # 分块避免单次绑定参数过多
            for start in range(0, len(ids), _CHUNK):
                chunk = ids[start:start + _CHUNK]
                placeholders = ", ".join("?" for _ in chunk)
                sql = (f"SELECT {quoted} FROM {spec.table} "
                       f"WHERE product_id IN ({placeholders})")
                for row in conn.execute(sql, tuple(chunk)):
                    value = _row_to_value(row, cols, tag_cols)
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
