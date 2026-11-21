# -*- coding: utf-8 -*-
"""SQLite 写入层：建表 / 流式分批写 / 过期清理。

所有连接统一只设两条 PRAGMA：``journal_mode = WAL``、``synchronous = NORMAL``。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .schema import TableSchema


def _configure(conn: sqlite3.Connection) -> sqlite3.Cursor:
    """建表、写入共用的连接配置；返回游标，避免两处各拼一遍 PRAGMA。"""
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode = WAL")
    cur.execute("PRAGMA synchronous = NORMAL")
    return cur


@dataclass
class WriteStats:
    """一次 ``write()`` 的可观测信号。"""

    rows: int = 0             # 交给 executemany 的逻辑记录数
    batches: int = 0          # executemany 调用次数
    commits: int = 0          # commit 次数
    connections: int = 0      # 打开的 sqlite 连接数
    purged: int = 0           # 最近一次 purge_expired 删除的行数
    errors: list[tuple[str, str]] = field(default_factory=list)  # [(库路径, 异常文本)]


class SqliteSink:
    """满足 ``sftp_avl2db.protocols.RecordSink`` 的 SQLite 实现。"""

    def __init__(self, profile, layout, *, table_schema: TableSchema | None = None,
                 batch_size: int | None = None):
        self.profile = profile
        self.layout = layout
        self.schema = table_schema or TableSchema.from_profile(profile)
        # 用 ``is None`` 而不是 ``or``：显式传 0 / 空串必须能被校验拦住，
        # 不能被当成「未指定」悄悄回落到默认值。
        self.batch_size = int(profile.retention.batch_size
                              if batch_size is None else batch_size)
        if self.batch_size <= 0:
            raise ValueError(f"batch_size 必须为正整数，当前为 {self.batch_size}")
        self.stats = WriteStats()

    def _group_targets_by_db(self) -> dict[Path, list[str]]:
        """把布局枚举出的 ``(库, 表)`` 按库文件分组；建表与过期清理共用。"""
        groups: dict[Path, list[str]] = {}
        for db_path, table in self.layout.all_targets():
            groups.setdefault(Path(db_path), []).append(table)
        return groups

    # ---- 建表（幂等） ----
    def ensure_schema(self) -> None:
        """按布局枚举全部库表并建表；同一库文件只连一次、一次 commit。"""
        for db_path, tables in self._group_targets_by_db().items():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path))
            try:
                cur = _configure(conn)
                for t in tables:
                    cur.execute(self.schema.create_table_sql(t))
                conn.commit()
            finally:
                conn.close()

    # ---- 流式分批写 ----
    def _connect(self, db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        self.stats.connections += 1
        _configure(conn)
        return conn

    def _flush(self, conn: sqlite3.Connection, table: str, batch: list) -> None:
        if not batch:
            return
        conn.executemany(self.schema.insert_sql(table), batch)
        conn.commit()
        self.stats.batches += 1
        self.stats.commits += 1

    def write(self, filename: str, rows: Iterable[tuple], layout=None) -> int:
        """把解析器产出的记录按布局分桶、逐桶满批提交；返回写入记录数。

        ``layout`` 默认用构造时传入的布局（同一份库表定义）；
        显式传入可实现更细的替换，例如单测里注入假布局。
        """
        layout = self.layout if layout is None else layout
        self.stats = WriteStats()

        conns: dict[str, sqlite3.Connection] = {}
        batches: dict[tuple[str, str], list] = {}

        try:
            for row in rows:
                row = tuple(row)
                self.schema.validate_row(row)
                db_path, table = layout.target(filename, row)
                db_path = Path(db_path)
                db_key = str(db_path)
                bucket = (db_key, table)
                if bucket not in batches:
                    batches[bucket] = []
                    if db_key not in conns:
                        db_path.parent.mkdir(parents=True, exist_ok=True)
                        conns[db_key] = self._connect(db_path)
                batches[bucket].append(row)
                self.stats.rows += 1
                if len(batches[bucket]) >= self.batch_size:
                    self._flush(conns[db_key], table, batches[bucket])
                    batches[bucket].clear()

            # 末尾余量：逐桶提交（控损 128 张表各自提交自己的余量）
            for (db_key, table), batch in batches.items():
                if batch:
                    self._flush(conns[db_key], table, batch)
                    batch.clear()

            return self.stats.rows
        except Exception:
            for conn in conns.values():
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            for conn in conns.values():
                conn.close()
            conns.clear()
            batches.clear()

    # ---- 过期清理 ----
    def purge_expired(self, cutoff: str,
                      tables: Iterable[str] | None = None) -> int:
        """按 ``op_time < cutoff`` 清理库表，返回删除总行数。

        ``tables=None`` → 清理布局枚举出的全部库表（旧行为）；传入表名集合
        → 只清这批表，用于「每天轮转清一部分分表」，避免每轮全库扫描。

        库文件不存在则跳过；异常时 rollback、记录到 ``stats.errors`` 并
        继续下一个库，**不抛给上层**。清理结果仍写进 ``stats.purged``。
        """
        total = 0
        self.stats.purged = 0
        self.stats.errors = []

        groups = self._group_targets_by_db()
        if tables is not None:
            picked = set(tables)
            groups = {db: [t for t in ts if t in picked]
                      for db, ts in groups.items()}
            # 本轮没有命中任何表时连 DB 都不打开
            groups = {db: ts for db, ts in groups.items() if ts}

        for db_path, targets in groups.items():
            if not db_path.is_file():
                continue
            conn = None
            try:
                conn = sqlite3.connect(str(db_path))
                cur = conn.cursor()
                for t in targets:
                    cur.execute(self.schema.delete_expired_sql(t), (cutoff,))
                    total += max(cur.rowcount, 0)
                conn.commit()
            except Exception as e:
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                self.stats.errors.append((str(db_path), str(e)))
            finally:
                if conn is not None:
                    conn.close()

        self.stats.purged = total
        return total


def build_sink(profile, *, layout=None, table_schema: TableSchema | None = None,
               batch_size: int | None = None) -> SqliteSink:
    """装配写入层；与 ``build_layout()`` 共用同一个 layout 对象。"""
    if layout is None:
        from .router import build_layout
        layout = build_layout(profile)
    return SqliteSink(profile, layout, table_schema=table_schema,
                      batch_size=batch_size)


__all__ = ["SqliteSink", "WriteStats", "build_sink"]
