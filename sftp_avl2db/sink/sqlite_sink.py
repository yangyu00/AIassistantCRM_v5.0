# -*- coding: utf-8 -*-
"""SQLite 写入层：建表 / 流式分批写 / 过期清理。

从 ``tasks/task_opportunity_recall.py`` 与 ``tasks/task_loss_control.py`` 的
``init_db()`` / ``parse_and_save()`` 写库部分 / ``_cleanup_db()`` 平移而来。

**逐字保留的既有差异**（不做统一，否则生产行为会变）：

| 项 | 商机 ``per_prefix`` | 控损 ``crc32_shards`` |
|---|---|---|
| ``PRAGMA synchronous`` | ``OFF`` | ``NORMAL`` |
| 批次容器 | 单表一个 ``batch`` list | 128 张表各一个 ``batches[t]`` list |
| 提交时机 | 满 ``BATCH_SIZE`` + 末尾余量 | 任一表满 ``BATCH_SIZE`` 就只提交该表 + 末尾逐表提交余量 |

新实现用「按 ``(库文件, 表)`` 分桶」统一表达上表两种形态：商机每文件只有
一个桶，控损每文件最多 128 个桶，行为与旧代码一致。

其余保真点：

- ``ensure_schema()`` 与 ``write()`` **各自建立连接**（旧 ``init_db()`` 与
  ``parse_and_save()`` 也是各连一次）；同一库文件在一次调用内只连一次。
- 连接 PRAGMA 顺序：``journal_mode=WAL`` → ``synchronous`` →
  ``cache_size=-64000`` → ``temp_store=MEMORY``。
- ``INSERT OR REPLACE`` + 表级 ``PRIMARY KEY (product_no)``，后写覆盖先写。
- ``parse()`` 是生成器，本层按 ``BATCH_SIZE`` 流式分桶，不把全量记录装进内存。
- 过期清理按 ``op_time < ?``，异常时 **rollback 并吞掉**（旧 ``_cleanup_db``
  是 ``log.error + 继续``，不抛给上层），错误收进 ``stats.errors`` 供编排器记录。

本层不写日志、不连网络、不读 ``.env``；只依赖标准库 ``sqlite3``。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .schema import TableSchema

# 现状差异，刻意不统一。注释标明来源，避免以后被「顺手优化」。
DEFAULT_SYNCHRONOUS = {
    "per_prefix": "OFF",       # tasks/task_opportunity_recall.py::parse_and_save
    "crc32_shards": "NORMAL",  # tasks/task_loss_control.py::parse_and_save
    "fixed_table": "NORMAL",   # 预留模式，与控损一致
}


@dataclass
class WriteStats:
    """一次 ``write()`` 的可观测信号（替代旧代码里的逐批 ``log.info``）。"""

    rows: int = 0             # 交给 executemany 的逻辑记录数
    batches: int = 0          # executemany 调用次数
    commits: int = 0          # commit 次数
    connections: int = 0      # 打开的 sqlite 连接数
    purged: int = 0           # 最近一次 purge_expired 删除的行数
    errors: list = field(default_factory=list)   # [(库路径, 异常文本)]


class SqliteSink:
    """满足 ``sftp_avl2db.protocols.RecordSink`` 的 SQLite 实现。"""

    def __init__(self, profile, layout, *, table_schema: TableSchema | None = None,
                 batch_size: int | None = None, synchronous: str | None = None):
        self.profile = profile
        self.layout = layout
        self.schema = table_schema or TableSchema.from_profile(profile)
        # 用 ``is None`` 而不是 ``or``：显式传 0 / 空串必须能被校验拦住，
        # 不能被当成「未指定」悄悄回落到默认值。
        self.batch_size = int(profile.retention.batch_size
                              if batch_size is None else batch_size)
        if self.batch_size <= 0:
            raise ValueError(f"batch_size 必须为正整数，当前为 {self.batch_size}")
        self.synchronous = (DEFAULT_SYNCHRONOUS.get(profile.routing, "NORMAL")
                            if synchronous is None else synchronous)
        self.stats = WriteStats()

    # ---- 建表（幂等） ----
    def ensure_schema(self) -> None:
        """按布局枚举全部库表并建表；同一库文件只连一次、一次 commit。"""
        groups = {}
        for db_path, table in self.layout.all_targets():
            groups.setdefault(Path(db_path), []).append(table)

        for db_path, tables in groups.items():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode = WAL")
                for t in tables:
                    cur.execute(self.schema.create_table_sql(t))
                conn.commit()
            finally:
                conn.close()

    # ---- 流式分批写 ----
    def _connect(self, db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        self.stats.connections += 1
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute(f"PRAGMA synchronous = {self.synchronous}")
        cur.execute("PRAGMA cache_size = -64000")
        cur.execute("PRAGMA temp_store = MEMORY")
        return conn

    def _flush(self, conn: sqlite3.Connection, table: str, batch: list) -> None:
        if not batch:
            return
        conn.executemany(self.schema.insert_sql(table), batch)
        conn.commit()
        self.stats.batches += 1
        self.stats.commits += 1

    def write(self, filename: str, rows: Iterable[tuple], router=None) -> int:
        """把解析器产出的记录按布局分桶、逐桶满批提交；返回写入记录数。

        ``router`` 默认用构造时传入的 ``layout``（同一份库表定义）；
        显式传入可实现更细的替换，例如单测里注入假路由。
        """
        router = self.layout if router is None else router
        self.stats = WriteStats()

        conns: dict[str, sqlite3.Connection] = {}
        batches: dict[tuple[str, str], list] = {}

        try:
            for row in rows:
                row = tuple(row)
                self.schema.validate_row(row)
                db_path, table = router.target(filename, row)
                db_path = Path(db_path)
                key = (str(db_path), table)
                if key not in batches:
                    batches[key] = []
                    if key[0] not in conns:
                        db_path.parent.mkdir(parents=True, exist_ok=True)
                        conns[key[0]] = self._connect(db_path)
                batches[key].append(row)
                self.stats.rows += 1
                if len(batches[key]) >= self.batch_size:
                    self._flush(conns[key[0]], table, batches[key])
                    batches[key].clear()

            # 末尾余量：逐桶提交（控损 128 张表各自提交自己的余量）
            for (db_path_str, table), batch in batches.items():
                if batch:
                    self._flush(conns[db_path_str], table, batch)
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
    def purge_expired(self, cutoff: str) -> int:
        """按 ``op_time < cutoff`` 清理全部库表，返回删除总行数。

        与原 ``_cleanup_db()`` 一致：库文件不存在则跳过；异常时 rollback、
        记录到 ``stats.errors`` 并继续下一个库，**不抛给上层**。
        """
        total = 0
        self.stats.purged = 0
        self.stats.errors = []

        groups = {}
        for db_path, table in self.layout.all_targets():
            groups.setdefault(Path(db_path), []).append(table)

        for db_path, tables in groups.items():
            if not db_path.is_file():
                continue
            conn = None
            try:
                conn = sqlite3.connect(str(db_path))
                cur = conn.cursor()
                for t in tables:
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


def build_sink(profile, *, layout=None, **kwargs) -> SqliteSink:
    """装配写入层；与 ``build_layout()`` 共用同一个 layout 对象。"""
    from .router import build_layout
    return SqliteSink(profile, layout or build_layout(profile), **kwargs)


__all__ = ["SqliteSink", "WriteStats", "DEFAULT_SYNCHRONOUS", "build_sink"]
