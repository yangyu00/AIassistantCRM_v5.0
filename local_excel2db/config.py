# -*- coding: utf-8 -*-
"""配置：唯一来源 ``local_excel2db/registry.json``，唯一入口 ``load_registry()``。"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import ColumnSpec, Registry, TableSpec

MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parent
INPUT_DIR = PROJECT_ROOT / "excels"                 # 唯一输入目录（项目根）
REGISTRY_PATH = MODULE_DIR / "registry.json"        # 唯一配置来源
RUNTIME_DIR = MODULE_DIR / "runtime"                # 唯一产出目录
DB_PATH = RUNTIME_DIR / "sqlite_db" / "dimensions.db"
STATE_DIR = RUNTIME_DIR / "state"
LOCK_PATH = STATE_DIR / "dim_watcher.lock"
LOG_DIR = RUNTIME_DIR / "log"

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPES = frozenset({"TEXT", "INTEGER", "REAL"})


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _name(value: Any) -> bool:
    return isinstance(value, str) and bool(_NAME_RE.match(value))


def _source_path(value: Any, table: str) -> Path:
    """把 registry 的 ``source`` 解析到 ``INPUT_DIR``。"""
    _need(isinstance(value, str) and bool(value), f"{table}: source 不能为空")
    _need(not Path(value).is_absolute() and "/" not in value and "\\" not in value
          and value not in (".", ".."),
          f"{table}: source 只能是纯文件名，恒解析到 {INPUT_DIR}，"
          f"不接受路径或绝对路径: {value!r}")
    return INPUT_DIR / value


def load_registry() -> Registry:
    """读包内 ``registry.json`` 并冻结成 ``Registry``。"""
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return _build(data)


def _build(data: Mapping[str, Any]) -> Registry:
    _need(isinstance(data, Mapping), "registry 顶层必须是对象")

    interval = int(data.get("poll_interval_seconds", 60))
    _need(interval > 0, "poll_interval_seconds 必须为正整数")

    version_column = data.get("version_column", "version")
    meta_table = data.get("meta_table", "dim_meta")
    _need(_name(version_column), f"version_column 非法: {version_column}")
    _need(_name(meta_table), f"meta_table 非法: {meta_table}")

    raw_tables = data.get("tables")
    _need(isinstance(raw_tables, list) and raw_tables, "tables 必须是非空数组")

    seen_tables: set[str] = set()
    tables = [
        _table(t, version_column, meta_table, seen_tables)
        for t in raw_tables
    ]
    return Registry(
        config_path=REGISTRY_PATH,
        input_dir=INPUT_DIR,
        runtime_dir=RUNTIME_DIR,
        database=DB_PATH,
        state_dir=STATE_DIR,
        lock_path=LOCK_PATH,
        log_dir=LOG_DIR,
        poll_interval_seconds=interval,
        version_column=version_column,
        meta_table=meta_table,
        tables=tuple(tables),
    )


def _table(t: Any, version_column: str, meta_table: str,
           seen_tables: set[str]) -> TableSpec:
    _need(isinstance(t, Mapping), "tables[] 元素必须是对象")
    name = t.get("table")
    _need(_name(name), f"表名非法: {name}")
    _need(name != meta_table, f"表名不能与 meta_table 相同: {name}")
    _need(name not in seen_tables, f"表名重复: {name}")
    seen_tables.add(name)

    src = _source_path(t.get("source"), name)
    sheet = t.get("sheet", 0)
    _need(isinstance(sheet, (int, str)), f"{name}: sheet 需为序号或名称")
    header_row = int(t.get("header_row", 1))
    _need(header_row >= 1, f"{name}: header_row 需 >= 1")

    raw_cols = t.get("columns")
    _need(isinstance(raw_cols, list) and raw_cols, f"{name}: columns 必须是非空数组")
    seen_cols: set[str] = set()
    columns = [
        _column(c, name, version_column, seen_cols)
        for c in raw_cols
    ]
    dyn_cols = _dynamic_columns(
        t.get("dynamic_columns"), name, version_column, columns, seen_cols)

    pk = t.get("primary_key")
    _need(isinstance(pk, list) and pk, f"{name}: primary_key 必须是非空数组")
    _need(all(_name(x) for x in pk), f"{name}: primary_key 含非法列名")
    _need(len(set(pk)) == len(pk), f"{name}: primary_key 含重复列")
    _need(set(pk) <= seen_cols, f"{name}: primary_key 必须来自 columns 中的列名")
    by_name = {c.column: c for c in columns}
    _need(all(not by_name[k].optional for k in pk),
          f"{name}: 主键列不能设为 optional")

    return TableSpec(
        table=name,
        source=src,
        sheet=sheet,
        header_row=header_row,
        primary_key=tuple(pk),
        columns=tuple(columns),
        dynamic_columns=tuple(dyn_cols),
    )


def _column(c: Any, table: str, version_column: str,
            seen_cols: set[str]) -> ColumnSpec:
    _need(isinstance(c, Mapping), f"{table}: columns[] 元素必须是对象")
    col = c.get("column")
    _need(_name(col), f"{table}: 列名非法 {col}")
    _need(col != version_column, f"{table}: 列名不能与 version_column 相同: {col}")
    _need(col not in seen_cols, f"{table}: 列名重复 {col}")
    seen_cols.add(col)

    source, index = c.get("source"), c.get("column_index")
    _need(source or index, f"{table}.{col}: 需提供 source 或 column_index")
    if index is not None:
        index = int(index)
        _need(index >= 1, f"{table}.{col}: column_index 需 >= 1")
    typ = str(c.get("type", "TEXT")).upper()
    _need(typ in _TYPES, f"{table}.{col}: 不支持的类型 {typ}")
    return ColumnSpec(
        source=source,
        column_index=index,
        column=col,
        type=typ,
        default=str(c.get("default", "")),
        strip=bool(c.get("strip", True)),
        optional=bool(c.get("optional", False)),
    )


def _dynamic_columns(dyn: Any, table: str, version_column: str,
                     columns: list[ColumnSpec], seen_cols: set[str]) -> list[str]:
    """把「标签0…标签N」编号表头展开成普通列，列值原样入库。"""
    if dyn is None:
        return []
    _need(isinstance(dyn, Mapping), f"{table}: dynamic_columns 需为对象")
    sp, cp = dyn.get("source_prefix"), dyn.get("column_prefix")
    _need(isinstance(sp, str) and sp, f"{table}: dynamic_columns.source_prefix 非法")
    _need(_name(cp), f"{table}: dynamic_columns.column_prefix 非法")
    start, count = int(dyn.get("start", 0)), int(dyn.get("count", 0))
    _need(0 <= start <= 10000, f"{table}: dynamic_columns.start 非法")
    _need(0 < count <= 200, f"{table}: dynamic_columns.count 需为 1..200")

    out: list[str] = []
    for g in range(start, start + count):
        col = f"{cp}{g}"
        _need(_name(col) and col != version_column, f"{table}: 动态列名非法 {col}")
        _need(col not in seen_cols, f"{table}: 动态列名与已有列冲突 {col}")
        seen_cols.add(col)
        columns.append(ColumnSpec(
            source=f"{sp}{g}", column_index=None, column=col,
            type="TEXT", default="", strip=True, optional=True,
        ))
        out.append(col)
    return out


__all__ = [
    "MODULE_DIR", "PROJECT_ROOT", "INPUT_DIR", "REGISTRY_PATH", "RUNTIME_DIR",
    "DB_PATH", "STATE_DIR", "LOCK_PATH", "LOG_DIR", "load_registry",
]
