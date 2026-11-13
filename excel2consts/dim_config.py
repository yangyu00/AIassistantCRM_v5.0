# -*- coding: utf-8 -*-
"""加载并校验 registry.json，把相对路径解析到 excel2consts 目录下。"""
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "registry.json"
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPES = {"TEXT", "INTEGER", "REAL"}


def _need(cond, msg):
    if not cond:
        raise ValueError(msg)


def _resolve(path_str):
    p = Path(path_str)
    return p if p.is_absolute() else BASE_DIR / p


def load_registry(path=None):
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    data = json.loads(cfg_path.read_text(encoding="utf-8"))

    interval = int(data.get("poll_interval_seconds", 60))
    _need(interval > 0, "poll_interval_seconds 必须为正整数")

    version_column = data.get("version_column", "version")
    meta_table = data.get("meta_table", "dim_meta")
    _need(_NAME_RE.match(version_column), f"version_column 非法: {version_column}")
    _need(_NAME_RE.match(meta_table), f"meta_table 非法: {meta_table}")

    raw_tables = data.get("tables")
    _need(isinstance(raw_tables, list) and raw_tables, "tables 必须是非空数组")

    specs, seen = [], set()
    for t in raw_tables:
        name = t.get("table")
        _need(isinstance(name, str) and _NAME_RE.match(name), f"表名非法: {name}")
        _need(name != meta_table, f"表名不能与 meta_table 相同: {name}")
        _need(name not in seen, f"表名重复: {name}")
        seen.add(name)

        src = t.get("source")
        _need(isinstance(src, str) and src, f"{name}: source 不能为空")
        sheet = t.get("sheet", 0)
        _need(isinstance(sheet, int) or isinstance(sheet, str), f"{name}: sheet 需为序号或名称")
        header_row = int(t.get("header_row", 1))
        _need(header_row >= 1, f"{name}: header_row 需 >= 1")

        raw_cols = t.get("columns")
        _need(isinstance(raw_cols, list) and raw_cols, f"{name}: columns 必须是非空数组")
        columns, seen_cols = [], set()
        for c in raw_cols:
            col = c.get("column")
            _need(isinstance(col, str) and _NAME_RE.match(col), f"{name}: 列名非法 {col}")
            _need(col != version_column, f"{name}: 列名不能与 version_column 相同: {col}")
            _need(col not in seen_cols, f"{name}: 列名重复 {col}")
            seen_cols.add(col)
            source, index = c.get("source"), c.get("column_index")
            _need(source or index, f"{name}.{col}: 需提供 source 或 column_index")
            if index is not None:
                index = int(index)
                _need(index >= 1, f"{name}.{col}: column_index 需 >= 1")
            typ = str(c.get("type", "TEXT")).upper()
            _need(typ in _TYPES, f"{name}.{col}: 不支持的类型 {typ}")
            columns.append({
                "source": source,
                "column_index": index,
                "column": col,
                "type": typ,
                "default": str(c.get("default", "")),
                "strip": bool(c.get("strip", True)),
                "optional": bool(c.get("optional", False)),
            })

        pk = t.get("primary_key")
        _need(isinstance(pk, list) and pk, f"{name}: primary_key 必须是非空数组")
        _need(all(isinstance(x, str) and _NAME_RE.match(x) for x in pk),
              f"{name}: primary_key 含非法列名")
        _need(len(set(pk)) == len(pk), f"{name}: primary_key 含重复列")
        _need(set(pk) <= seen_cols, f"{name}: primary_key 必须来自 columns 中的列名")
        _need(all(not next(c for c in columns if c["column"] == k)["optional"] for k in pk),
              f"{name}: 主键列不能设为 optional")

        # 在线业务读取侧用的 {key: value} 映射声明（本次只有 label_name_mapping 用到）。
        lookup = t.get("lookup")
        if lookup is not None:
            _need(isinstance(lookup, dict), f"{name}: lookup 需为对象")
            key, value = lookup.get("key"), lookup.get("value")
            _need(isinstance(key, str) and _NAME_RE.match(key), f"{name}: lookup.key 非法")
            _need(isinstance(value, str) and _NAME_RE.match(value), f"{name}: lookup.value 非法")
            _need(key in seen_cols and value in seen_cols,
                  f"{name}: lookup 的 key/value 必须来自 columns 中的列名")
            lookup = {"key": key, "value": value}

        specs.append({
            "table": name,
            "source": _resolve(src),
            "sheet": sheet,
            "header_row": header_row,
            "primary_key": list(pk),
            "columns": columns,
            "lookup": lookup,
        })

    return {
        "config_path": cfg_path,
        "database": _resolve(data.get("database", "sqlite_db/dimensions.db")),
        "poll_interval_seconds": interval,
        "version_column": version_column,
        "meta_table": meta_table,
        "tables": specs,
    }
