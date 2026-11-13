# -*- coding: utf-8 -*-
"""按 registry 抽列写入 SQLite，并以 version 记录每次整体替换。"""
import hashlib
import sqlite3
from datetime import datetime

from openpyxl import load_workbook


class MissingColumn(Exception):
    """必需表头列缺失；由调用方记录并跳过本轮，下轮重试。"""


def fingerprint(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    st = path.stat()
    return {"sha256": h.hexdigest(), "size": st.st_size, "mtime": st.st_mtime}


def _cast(value, typ):
    if typ == "TEXT":
        return value
    try:
        return int(value) if typ == "INTEGER" else float(value)
    except (ValueError, TypeError):
        return 0 if typ == "INTEGER" else 0.0


def ensure_meta(conn, cfg):
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {cfg['meta_table']} ("
        "table_name TEXT PRIMARY KEY, version INTEGER NOT NULL, "
        "source_file TEXT NOT NULL, sha256 TEXT NOT NULL, "
        "file_size INTEGER, file_mtime REAL, row_count INTEGER, "
        "updated_at TEXT NOT NULL)"
    )
    conn.commit()


def ensure_table(conn, cfg, spec):
    ver = cfg["version_column"]
    pk = spec["primary_key"]
    defs = []
    for c in spec["columns"]:
        not_null = " NOT NULL" if c["column"] in pk else ""
        defs.append(f"{c['column']} {c['type']}{not_null}")
    defs.append(f"{ver} INTEGER NOT NULL")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {spec['table']} "
        f"({', '.join(defs)}, PRIMARY KEY ({', '.join(pk)}))"
    )
    conn.commit()

    actual = {r[1] for r in conn.execute(f"PRAGMA table_info({spec['table']})").fetchall()}
    expected = {c["column"] for c in spec["columns"]} | {ver}
    if actual != expected:
        raise RuntimeError(
            f"表 {spec['table']} 结构与 registry.json 不一致，请删除 "
            f"{cfg['database']} 后重建。现有={sorted(actual)} 期望={sorted(expected)}"
        )


def read_rows(spec, cfg):
    """返回按主键去重后的行值列表；主键为空的行跳过。"""
    wb = load_workbook(spec["source"], read_only=True, data_only=True)
    try:
        ws = wb[spec["sheet"]] if isinstance(spec["sheet"], str) else wb.worksheets[spec["sheet"]]
        rows = ws.iter_rows(values_only=True)
        header = None
        for i, row in enumerate(rows, 1):
            if i == spec["header_row"]:
                header = ["" if v is None else str(v).strip() for v in row]
                break
        if header is None:
            raise RuntimeError(f"表头行 {spec['header_row']} 不存在")

        positions = []
        for c in spec["columns"]:
            if c["column_index"] is not None:
                pos = c["column_index"] - 1
                if pos >= len(header):
                    raise RuntimeError(f"column_index {c['column_index']} 超出表头列数")
            elif c["source"] in header:
                pos = header.index(c["source"])
            elif c["optional"]:
                pos = None
            else:
                raise MissingColumn(f"缺少必需表头列 [{c['source']}]")
            positions.append(pos)

        pk_idx = [next(i for i, c in enumerate(spec["columns"]) if c["column"] == k)
                  for k in spec["primary_key"]]
        out, skipped_empty, duplicated = {}, 0, 0
        for row in rows:
            values = []
            for c, pos in zip(spec["columns"], positions):
                raw = "" if pos is None or pos >= len(row) or row[pos] is None else row[pos]
                val = str(raw)
                if c["strip"]:
                    val = val.strip()
                if not val and c["default"]:
                    val = c["default"]
                values.append(_cast(val, c["type"]))
            key = tuple(values[i] for i in pk_idx)
            if any(v is None or str(v).strip() == "" for v in key):
                skipped_empty += 1
                continue
            if key in out:
                duplicated += 1
            out[key] = values
        return {
            "rows": list(out.values()),
            "skipped_empty": skipped_empty,
            "duplicated": duplicated,
            "header": header,
        }
    finally:
        wb.close()


def import_file(conn, cfg, spec, force=False):
    """指纹未变则跳过；否则整表替换并递增 version。返回状态字典。"""
    path = spec["source"]
    if not path.is_file():
        return {"status": "missing", "path": str(path)}

    fp = fingerprint(path)
    ensure_meta(conn, cfg)
    ensure_table(conn, cfg, spec)

    meta = cfg["meta_table"]
    prev = conn.execute(
        f"SELECT version, sha256 FROM {meta} WHERE table_name=?", (spec["table"],)
    ).fetchone()
    if prev and not force and prev[1] == fp["sha256"]:
        return {"status": "unchanged", "version": prev[0]}

    parsed = read_rows(spec, cfg)
    version = (prev[0] if prev else 0) + 1
    cols = [c["column"] for c in spec["columns"]] + [cfg["version_column"]]
    sql = (
        f"INSERT OR REPLACE INTO {spec['table']} ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})"
    )
    payload = [tuple(r) + (version,) for r in parsed["rows"]]

    try:
        conn.execute(f"DELETE FROM {spec['table']}")
        conn.executemany(sql, payload)
        conn.execute(
            f"INSERT OR REPLACE INTO {meta} "
            "(table_name, version, source_file, sha256, file_size, file_mtime, row_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (spec["table"], version, str(path), fp["sha256"], fp["size"], fp["mtime"],
             len(payload), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "status": "updated",
        "version": version,
        "rows": len(payload),
        "skipped_empty": parsed["skipped_empty"],
        "duplicated": parsed["duplicated"],
        "sha256": fp["sha256"][:12],
        "path": str(path),
    }
