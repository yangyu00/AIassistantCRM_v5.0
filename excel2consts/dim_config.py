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

        # 动态列：把 Excel 里「标签0…标签N」这类编号列按声明展开成普通列，
        # 库里存每列的原始文本，拆解加工留给读取侧（dim_transform）。
        dyn = t.get("dynamic_columns")
        dyn_cols = []
        if dyn is not None:
            _need(isinstance(dyn, dict), f"{name}: dynamic_columns 需为对象")
            sp, cp = dyn.get("source_prefix"), dyn.get("column_prefix")
            _need(isinstance(sp, str) and sp, f"{name}: dynamic_columns.source_prefix 非法")
            _need(isinstance(cp, str) and _NAME_RE.match(cp),
                  f"{name}: dynamic_columns.column_prefix 非法")
            start, count = int(dyn.get("start", 0)), int(dyn.get("count", 0))
            _need(0 <= start <= 10000, f"{name}: dynamic_columns.start 非法")
            _need(0 < count <= 200, f"{name}: dynamic_columns.count 需为 1..200")
            for g in range(start, start + count):
                col = f"{cp}{g}"
                _need(_NAME_RE.match(col) and col != version_column,
                      f"{name}: 动态列名非法 {col}")
                _need(col not in seen_cols, f"{name}: 动态列名与已有列冲突 {col}")
                seen_cols.add(col)
                columns.append({
                    "source": f"{sp}{g}", "column_index": None, "column": col,
                    "type": "TEXT", "default": "", "strip": True, "optional": True,
                })
                dyn_cols.append(col)

        pk = t.get("primary_key")
        _need(isinstance(pk, list) and pk, f"{name}: primary_key 必须是非空数组")
        _need(all(isinstance(x, str) and _NAME_RE.match(x) for x in pk),
              f"{name}: primary_key 含非法列名")
        _need(len(set(pk)) == len(pk), f"{name}: primary_key 含重复列")
        _need(set(pk) <= seen_cols, f"{name}: primary_key 必须来自 columns 中的列名")
        _need(all(not next(c for c in columns if c["column"] == k)["optional"] for k in pk),
              f"{name}: 主键列不能设为 optional")

        # 在线业务读取侧的映射声明。
        #   value : 单列  -> {key: 单值}        如 label_name_mapping
        #   values: 多列  -> {key: (值1, 值2…)} 如 product_selling_point_match
        lookup = t.get("lookup")
        if lookup is not None:
            _need(isinstance(lookup, dict), f"{name}: lookup 需为对象")
            key = lookup.get("key")
            _need(isinstance(key, str) and _NAME_RE.match(key), f"{name}: lookup.key 非法")
            _need(key in seen_cols, f"{name}: lookup.key 必须来自 columns 中的列名")

            single, multi = lookup.get("value"), lookup.get("values")
            _need((single is None) != (multi is None),
                  f"{name}: lookup 需且只能提供 value 或 values 之一")
            if single is not None:
                _need(isinstance(single, str) and _NAME_RE.match(single),
                      f"{name}: lookup.value 非法")
                _need(single in seen_cols and single != key,
                      f"{name}: lookup.value 必须来自 columns 且不等于 key")
                lookup = {"key": key, "value": single, "values": None}
            else:
                _need(isinstance(multi, list) and multi,
                      f"{name}: lookup.values 需为非空数组")
                _need(all(isinstance(v, str) and _NAME_RE.match(v) for v in multi),
                      f"{name}: lookup.values 含非法列名")
                _need(len(set(multi)) == len(multi), f"{name}: lookup.values 含重复列")
                _need(all(v in seen_cols and v != key for v in multi),
                      f"{name}: lookup.values 必须来自 columns 且不等于 key")
                lookup = {"key": key, "value": None, "values": list(multi)}

        # cut_in_script：声明读取侧重建 {business_id: {标签ID: [切入, 挽留]}}
        # 所需的原始列角色。库里不存加工结果，只存下列原始列。
        ci = t.get("cut_in_script")
        if ci is not None:
            _need(isinstance(ci, dict), f"{name}: cut_in_script 需为对象")
            for role in ("key", "fallback", "groups"):
                v = ci.get(role)
                _need(isinstance(v, str) and _NAME_RE.match(v) and v in seen_cols,
                      f"{name}: cut_in_script.{role} 必须来自 columns 中的列名")
            raw_tags = ci.get("tags")
            if raw_tags == "dynamic_columns":
                _need(dyn_cols, f"{name}: cut_in_script.tags 引用了 dynamic_columns，但未声明")
                tag_cols = list(dyn_cols)
            else:
                _need(isinstance(raw_tags, list) and raw_tags,
                      f"{name}: cut_in_script.tags 需为列名数组或 \"dynamic_columns\"")
                _need(all(isinstance(v, str) and _NAME_RE.match(v) and v in seen_cols
                          for v in raw_tags),
                      f"{name}: cut_in_script.tags 含未声明列名")
                _need(len(set(raw_tags)) == len(raw_tags),
                      f"{name}: cut_in_script.tags 含重复列")
                tag_cols = list(raw_tags)
            fk = ci.get("fallback_key", "兜底话术")
            _need(isinstance(fk, str) and fk, f"{name}: cut_in_script.fallback_key 不能为空")
            ci = {
                "key": ci["key"], "fallback": ci["fallback"], "groups": ci["groups"],
                "tags": tag_cols, "fallback_key": fk,
            }

        specs.append({
            "table": name,
            "source": _resolve(src),
            "sheet": sheet,
            "header_row": header_row,
            "primary_key": list(pk),
            "columns": columns,
            "dynamic_columns": dyn_cols,
            "lookup": lookup,
            "cut_in_script": ci,
        })

    return {
        "config_path": cfg_path,
        "database": _resolve(data.get("database", "sqlite_db/dimensions.db")),
        "poll_interval_seconds": interval,
        "version_column": version_column,
        "meta_table": meta_table,
        "tables": specs,
    }
