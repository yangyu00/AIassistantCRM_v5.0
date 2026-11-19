# -*- coding: utf-8 -*-
"""SQLite 落地：写入侧建表 / 单事务整表覆盖，读取侧只读查询。

SQL 与 version 语义平移自旧 ``dim_importer``：库里只存 Excel 原始列 + 每行
``version``；``dim_meta`` 按 ``table_name`` 记录指纹与版本，按表独立递增。
整表覆盖在同一事务内完成，任一环节失败整体回滚、指纹不更新、下轮自动重试。

表名 / 列名已在 ``RegistryLoader`` 用 ``_NAME_RE`` 白名单校验后拼接（SQLite 不
支持参数化表名），本模块不重复校验。``connection_factory`` 可注入，便于离线
测试或在特殊文件系统上替换连接方式。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from collections.abc import Callable, Sequence

from .models import Registry, TableSpec
from .protocols import FileFingerprint, ParsedRows, Row, SchemaMismatch


class SqliteDimensionRepository:
    """一个实例对应一个库连接；写模式懒建目录，读模式用 ``mode=ro``。"""

    def __init__(self, registry: Registry, *, readonly: bool = False,
                 connection_factory: Callable[[], sqlite3.Connection] | None = None) -> None:
        self._registry = registry
        self._readonly = readonly
        self._factory = connection_factory
        self._conn: sqlite3.Connection | None = None

    def _open(self) -> sqlite3.Connection:
        if self._factory is not None:
            return self._factory()
        if self._readonly:
            return sqlite3.connect(f"{self._registry.database.as_uri()}?mode=ro", uri=True)
        self._registry.database.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self._registry.database))

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- 写入侧 ----------------------------------------------------------

    def ensure_meta(self) -> None:
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._registry.meta_table} ("
            "table_name TEXT PRIMARY KEY, version INTEGER NOT NULL, "
            "source_file TEXT NOT NULL, sha256 TEXT NOT NULL, "
            "file_size INTEGER, file_mtime REAL, row_count INTEGER, "
            "updated_at TEXT NOT NULL)"
        )
        self.conn.commit()

    def ensure_table(self, spec: TableSpec) -> None:
        ver = self._registry.version_column
        defs = []
        for c in spec.columns:
            not_null = " NOT NULL" if c.column in spec.primary_key else ""
            defs.append(f"{c.column} {c.type}{not_null}")
        defs.append(f"{ver} INTEGER NOT NULL")
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {spec.table} "
            f"({', '.join(defs)}, PRIMARY KEY ({', '.join(spec.primary_key)}))"
        )
        self.conn.commit()

        actual = {r[1] for r in self.conn.execute(
            f"PRAGMA table_info({spec.table})").fetchall()}
        expected = set(spec.column_names) | {ver}
        if actual != expected:
            raise SchemaMismatch(
                f"表 {spec.table} 结构与 registry.json 不一致，请删除 "
                f"{self._registry.database} 后重建。"
                f"现有={sorted(actual)} 期望={sorted(expected)}"
            )

    def previous_version(self, spec: TableSpec) -> tuple[int, str] | None:
        row = self.conn.execute(
            f"SELECT version, sha256 FROM {self._registry.meta_table} WHERE table_name=?",
            (spec.table,),
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def replace_all(self, spec: TableSpec, parsed: ParsedRows, version: int,
                    fingerprint: FileFingerprint, source_path: Path) -> None:
        reg = self._registry
        cols = list(spec.column_names) + [reg.version_column]
        sql = (
            f"INSERT OR REPLACE INTO {spec.table} ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})"
        )
        payload = [r + (version,) for r in parsed.rows]
        meta = reg.meta_table
        try:
            self.conn.execute(f"DELETE FROM {spec.table}")
            self.conn.executemany(sql, payload)
            self.conn.execute(
                f"INSERT OR REPLACE INTO {meta} "
                "(table_name, version, source_file, sha256, file_size, "
                "file_mtime, row_count, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (spec.table, version, str(source_path), fingerprint.sha256,
                 fingerprint.size, fingerprint.mtime, len(payload),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- 读取侧 ----------------------------------------------------------

    def query(self, table: str, columns: Sequence[str],
              *, order_by_rowid: bool = False) -> list[Row]:
        sql = f"SELECT {', '.join(columns)} FROM {table}"
        if order_by_rowid:
            sql += " ORDER BY rowid"
        return self.conn.execute(sql).fetchall()


__all__ = ["SqliteDimensionRepository"]
