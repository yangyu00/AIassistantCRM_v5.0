# -*- coding: utf-8 -*-
"""加载并校验 ``registry.json``，产出不可变的 ``Registry`` 对象。

路径基准：所有相对路径（database / source）相对 **registry 文件所在目录** 解析；
``from_mapping`` 未给 config_path 时相对当前工作目录。其他项目用 ``--config``
指一份配置即可，不必把 xlsx 或库放进包目录。

import 本模块零 I/O：只有显式调用 ``from_file`` / ``from_mapping`` 才读文件。
校验规则平移自旧 ``dim_config.load_registry``，不合法直接抛 ``ValueError``。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import ColumnSpec, CutInScriptSpec, LookupSpec, Registry, TableSpec

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPES = frozenset({"TEXT", "INTEGER", "REAL"})


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _name(value: Any) -> bool:
    return isinstance(value, str) and bool(_NAME_RE.match(value))


def _resolve(base: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else base / p


class RegistryLoader:
    """把 registry 的 JSON 文本 / 内存 dict 转成已校验的 Registry。

    ``base_dir`` 显式给出时覆盖默认基准，便于从内存构造配置的宿主项目。
    """

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base_dir = Path(base_dir).resolve() if base_dir is not None else None

    @staticmethod
    def default_config() -> Path:
        return Path(__file__).resolve().parent / "registry.json"

    def from_file(self, path: Path | str | None = None) -> Registry:
        cfg_path = Path(path).resolve() if path is not None else self.default_config()
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return self._build(data, cfg_path=cfg_path,
                           base_dir=self._base_dir or cfg_path.parent)

    def from_mapping(self, data: Mapping[str, Any], *,
                     config_path: Path | str | None = None) -> Registry:
        cfg_path = Path(config_path).resolve() if config_path is not None else None
        base = self._base_dir or (cfg_path.parent if cfg_path else Path.cwd().resolve())
        return self._build(data, cfg_path=cfg_path, base_dir=base)

    def _build(self, data: Mapping[str, Any], *, cfg_path: Path | None,
               base_dir: Path) -> Registry:
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
            self._table(t, base_dir, version_column, meta_table, seen_tables)
            for t in raw_tables
        ]
        return Registry(
            config_path=cfg_path,
            base_dir=base_dir,
            database=_resolve(base_dir, data.get("database", "sqlite_db/dimensions.db")),
            poll_interval_seconds=interval,
            version_column=version_column,
            meta_table=meta_table,
            tables=tuple(tables),
        )

    @staticmethod
    def _table(t: Any, base_dir: Path, version_column: str, meta_table: str,
               seen_tables: set[str]) -> TableSpec:
        _need(isinstance(t, Mapping), "tables[] 元素必须是对象")
        name = t.get("table")
        _need(_name(name), f"表名非法: {name}")
        _need(name != meta_table, f"表名不能与 meta_table 相同: {name}")
        _need(name not in seen_tables, f"表名重复: {name}")
        seen_tables.add(name)

        src = t.get("source")
        _need(isinstance(src, str) and src, f"{name}: source 不能为空")
        sheet = t.get("sheet", 0)
        _need(isinstance(sheet, (int, str)), f"{name}: sheet 需为序号或名称")
        header_row = int(t.get("header_row", 1))
        _need(header_row >= 1, f"{name}: header_row 需 >= 1")

        raw_cols = t.get("columns")
        _need(isinstance(raw_cols, list) and raw_cols, f"{name}: columns 必须是非空数组")
        seen_cols: set[str] = set()
        columns = [
            RegistryLoader._column(c, name, version_column, seen_cols)
            for c in raw_cols
        ]
        dyn_cols = RegistryLoader._dynamic_columns(
            t.get("dynamic_columns"), name, version_column, columns, seen_cols)

        pk = t.get("primary_key")
        _need(isinstance(pk, list) and pk, f"{name}: primary_key 必须是非空数组")
        _need(all(_name(x) for x in pk), f"{name}: primary_key 含非法列名")
        _need(len(set(pk)) == len(pk), f"{name}: primary_key 含重复列")
        _need(set(pk) <= seen_cols, f"{name}: primary_key 必须来自 columns 中的列名")
        by_name = {c.column: c for c in columns}
        _need(all(not by_name[k].optional for k in pk),
              f"{name}: 主键列不能设为 optional")

        lookup = RegistryLoader._lookup(t.get("lookup"), name, seen_cols)
        cut_in = RegistryLoader._cut_in_script(
            t.get("cut_in_script"), name, seen_cols, dyn_cols)

        return TableSpec(
            table=name,
            source=_resolve(base_dir, src),
            sheet=sheet,
            header_row=header_row,
            primary_key=tuple(pk),
            columns=tuple(columns),
            dynamic_columns=tuple(dyn_cols),
            lookup=lookup,
            cut_in_script=cut_in,
        )

    @staticmethod
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

    @staticmethod
    def _dynamic_columns(dyn: Any, table: str, version_column: str,
                         columns: list[ColumnSpec], seen_cols: set[str]) -> list[str]:
        """把「标签0…标签N」编号表头展开成普通列，列值原样入库，加工留给读取侧。"""
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

    @staticmethod
    def _lookup(raw: Any, table: str, seen_cols: set[str]) -> LookupSpec | None:
        if raw is None:
            return None
        _need(isinstance(raw, Mapping), f"{table}: lookup 需为对象")
        key = raw.get("key")
        _need(_name(key), f"{table}: lookup.key 非法")
        _need(key in seen_cols, f"{table}: lookup.key 必须来自 columns 中的列名")

        single, multi = raw.get("value"), raw.get("values")
        _need((single is None) != (multi is None),
              f"{table}: lookup 需且只能提供 value 或 values 之一")
        if single is not None:
            _need(_name(single), f"{table}: lookup.value 非法")
            _need(single in seen_cols and single != key,
                  f"{table}: lookup.value 必须来自 columns 且不等于 key")
            return LookupSpec(key=key, value=single)

        _need(isinstance(multi, list) and multi, f"{table}: lookup.values 需为非空数组")
        _need(all(_name(v) for v in multi), f"{table}: lookup.values 含非法列名")
        _need(len(set(multi)) == len(multi), f"{table}: lookup.values 含重复列")
        _need(all(v in seen_cols and v != key for v in multi),
              f"{table}: lookup.values 必须来自 columns 且不等于 key")
        return LookupSpec(key=key, values=tuple(multi))

    @staticmethod
    def _cut_in_script(raw: Any, table: str, seen_cols: set[str],
                       dyn_cols: list[str]) -> CutInScriptSpec | None:
        if raw is None:
            return None
        _need(isinstance(raw, Mapping), f"{table}: cut_in_script 需为对象")
        roles: dict[str, str] = {}
        for role in ("key", "fallback", "groups"):
            v = raw.get(role)
            _need(_name(v) and v in seen_cols,
                  f"{table}: cut_in_script.{role} 必须来自 columns 中的列名")
            roles[role] = v

        raw_tags = raw.get("tags")
        if raw_tags == "dynamic_columns":
            _need(dyn_cols, f"{table}: cut_in_script.tags 引用了 dynamic_columns，但未声明")
            tag_cols = list(dyn_cols)
        else:
            _need(isinstance(raw_tags, list) and raw_tags,
                  f'{table}: cut_in_script.tags 需为列名数组或 "dynamic_columns"')
            _need(all(_name(v) and v in seen_cols for v in raw_tags),
                  f"{table}: cut_in_script.tags 含未声明列名")
            _need(len(set(raw_tags)) == len(raw_tags),
                  f"{table}: cut_in_script.tags 含重复列")
            tag_cols = list(raw_tags)

        fk = raw.get("fallback_key", "兜底话术")
        _need(isinstance(fk, str) and fk, f"{table}: cut_in_script.fallback_key 不能为空")
        return CutInScriptSpec(
            key=roles["key"], fallback=roles["fallback"], groups=roles["groups"],
            tags=tuple(tag_cols), fallback_key=fk,
        )


__all__ = ["RegistryLoader"]
