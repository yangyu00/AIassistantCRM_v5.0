# -*- coding: utf-8 -*-
"""SQLite 落地：写入侧建表 / 单事务整表覆盖 / 版本登记（只写，不提供查询）。"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .models import Registry, TableSpec
from .protocols import FileFingerprint, ParsedRows, SchemaMismatch


class SqliteDimensionRepository:
    """一个实例对应一个库连接。"""

    def __init__(self, registry: Registry, *,
                 connection_factory: Callable[[], sqlite3.Connection] | None = None) -> None:
        self._registry = registry
        self._factory = connection_factory
        self._conn: sqlite3.Connection | None = None
        self._memory_journal = False

    def _open(self) -> sqlite3.Connection:
        if self._factory is not None:
            return self._factory()
        db = self._registry.database
        db.parent.mkdir(parents=True, exist_ok=True)
        if not db.exists():
            # 部分容器 / 网络挂载上，SQLite 自己「首次创建」库文件会瞬时 I/O 错误。
            db.touch()

        rounds = (True,) if self._memory_journal else (False, True)
        baseline: int | None = None
        for memory in rounds:
            conn = sqlite3.connect(str(db))
            try:
                if memory:
                    # 仅对降级后的连接生效：journal 放内存，绕开挂载不支持的同步。
                    conn.execute("PRAGMA journal_mode=MEMORY")
                v = conn.execute("PRAGMA user_version").fetchone()[0]
                if baseline is None:
                    baseline = v
                if memory:
                    # 修掉上一次探测失败时留在 header 上的 +1，保证零数据变更。
                    if v != baseline:
                        conn.execute(f"PRAGMA user_version = {baseline}")
                        conn.commit()
                else:
                    # 零数据变更的写探测（改完立即改回）：正常文件系统直接成功；
                    # 不支持 journal 同步的挂载会在这里抛 disk I/O error，于是下一轮
                    # 切到内存 journal。库已存在时同样探测，避免大事务才炸。
                    conn.execute(f"PRAGMA user_version = {baseline + 1}")
                    conn.execute(f"PRAGMA user_version = {baseline}")
                    conn.commit()
                self._memory_journal = memory
                return conn
            except sqlite3.OperationalError:
                conn.close()
                if memory:
                    raise
        raise RuntimeError("SQLite 连接建立失败")  # 理论不可达

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


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


__all__ = ["SqliteDimensionRepository"]
