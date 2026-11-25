"""SQLite 写入层：建表 / 流式分批写 / 过期清理。"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .schema import (
    TableSchema, create_meta_table_sql, upsert_meta_sql,
)

# 远程文件名 {AVL}{YYYYMMDD}000000.AVL：取末尾 000000 之前的 8 位日期。
_FILE_DATE_RE = re.compile(r"(\d{8})000000$")


def _configure(conn: sqlite3.Connection) -> sqlite3.Cursor:
    """建表、写入共用的连接配置。"""
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
    dropped_tables: int = 0   # 全量替换模式下本次 DROP 并重建的业务表数
    max_op_time: str = ""     # 本文件写入记录里最大的 op_time（原样文本）
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

    def _group_targets_by_db(self, layout=None) -> dict[Path, list[str]]:
        """把布局枚举出的 ``(库, 表)`` 按库文件分组。"""
        layout = self.layout if layout is None else layout
        groups: dict[Path, list[str]] = {}
        for db_path, table in layout.all_targets():
            groups.setdefault(Path(db_path), []).append(table)
        return groups

    def drop_before_insert_enabled(self) -> bool:
        """全量替换模式是否开启；profile 没带该字段时按关闭处理。"""
        retention = getattr(self.profile, "retention", None)
        return int(getattr(retention, "drop_before_insert", 0) or 0) == 1

    def drop_existing(self, layout=None) -> int:
        """DROP 本 AVL 的全部业务表，再按同一 DDL 立即重建（清掉历史数据）。

        ``_avl_meta`` 是批次水位表，不进 layout，保留不动；表名来自静态布局，
        不接受外部字符串。返回被重建的业务表数量。
        """
        layout = self.layout if layout is None else layout
        dropped = 0
        for db_path, tables in self._group_targets_by_db(layout).items():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path))
            try:
                cur = _configure(conn)
                for t in tables:
                    cur.execute(f"DROP TABLE IF EXISTS {t}")
                    cur.execute(self.schema.create_table_sql(t))
                    dropped += 1
                # 元数据表与业务表同库，幂等保留；不因业务表 DROP 丢失水位。
                cur.execute(create_meta_table_sql())
                conn.commit()
            finally:
                conn.close()
        return dropped

    def ensure_schema(self) -> None:
        """按布局枚举全部库表并建表。"""
        for db_path, tables in self._group_targets_by_db().items():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path))
            try:
                cur = _configure(conn)
                for t in tables:
                    cur.execute(self.schema.create_table_sql(t))
                # 元数据表与业务表同库，但不进 layout，不参与分表 / 过期清理。
                cur.execute(create_meta_table_sql())
                conn.commit()
            finally:
                conn.close()

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

    def _time_index(self) -> int:
        """``op_time`` 列下标；表结构没有时间列时返回 -1（不收集水位）。"""
        try:
            return self.schema.column_names.index(self.schema.time_column)
        except ValueError:
            return -1

    @staticmethod
    def _file_date(filename: str) -> str:
        """从 ``{AVL}{YYYYMMDD}000000.AVL`` 取文件名日期；无法识别返回空串。"""
        m = _FILE_DATE_RE.search(Path(filename).stem)
        return m.group(1) if m else ""

    def _record_metadata(self, filename: str, layout) -> None:
        """入库成功后推进 ``_avl_meta`` 水位。

        元数据写失败只记 ``stats.errors``：不回滚业务数据，也不阻断
        ``runner`` 登记清单（否则会触发无意义的重复拉取）。
        """
        file_date = self._file_date(filename)
        if not file_date:
            return
        targets = list(layout.all_targets())
        if not targets:
            return
        db_path = Path(targets[0][0])
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path))
            try:
                _configure(conn)
                # 幂等：即使调用方没先跑 ensure_schema() 也能登记。
                conn.execute(create_meta_table_sql())
                conn.execute(upsert_meta_sql(), (
                    self.profile.name,
                    file_date,
                    self.stats.max_op_time,
                    Path(filename).name,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            self.stats.errors.append((str(db_path), f"meta: {e}"))

    def write(self, filename: str, rows: Iterable[tuple], layout=None) -> int:
        """把解析器产出的记录按布局分桶、逐桶满批提交，并登记批次水位。"""
        layout = self.layout if layout is None else layout
        self.stats = WriteStats()
        time_index = self._time_index()

        # 全量替换模式：写入最新 AVL 之前先 DROP 并重建全部目标表，
        # 表里的历史数据（含上游不再下发的主键）一次性清干净。
        if self.drop_before_insert_enabled():
            self.stats.dropped_tables = self.drop_existing(layout)

        conns: dict[str, sqlite3.Connection] = {}
        batches: dict[tuple[str, str], list] = {}

        try:
            for row in rows:
                row = tuple(row)
                self.schema.validate_row(row)
                if time_index >= 0:
                    op_time = str(row[time_index])
                    if op_time > self.stats.max_op_time:
                        self.stats.max_op_time = op_time
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

        # 业务数据已全部提交且没有异常：才推进水位。
        self._record_metadata(filename, layout)
        return self.stats.rows

    def purge_expired(self, cutoff: str,
                      tables: Iterable[str] | None = None) -> int:
        """按 ``op_time < cutoff`` 清理库表，返回删除总行数。"""
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
                cur = _configure(conn)
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
    """装配写入层。"""
    if layout is None:
        from .router import build_layout
        layout = build_layout(profile)
    return SqliteSink(profile, layout, table_schema=table_schema,
                      batch_size=batch_size)


__all__ = ["SqliteSink", "WriteStats", "build_sink"]
