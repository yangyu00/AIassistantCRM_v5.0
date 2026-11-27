"""SQLite 写入层：建表 / 流式分批写 / 过期清理（F09633 导入优化版）。"""
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

# 全局未刷行数上限的默认值：分表数再多，内存里也只留这么多行。
# 10 万行是实测折中值：千万行 AVL 约 100 次提交，每批每张分表平均
# 780 行、局部性够好，常驻内存约 40~60MB；水位过小会把 128 张表切成
# 大量小批，过大则单事务 WAL 与内存都偏大（20 万水位实测反而略慢）。
DEFAULT_GLOBAL_FLUSH_ROWS = 100000


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
    rolled_rows: int = 0      # 最近一次 roll_forward_op_time() 更新的行数
    errors: list[tuple[str, str]] = field(default_factory=list)  # [(库路径, 异常文本)]


class SqliteSink:
    """满足 ``sftp_avl2db.protocols.RecordSink`` 的 SQLite 实现。"""

    def __init__(self, profile, layout, *, table_schema: TableSchema | None = None,
                 batch_size: int | None = None,
                 global_flush_rows: int | None = None,
                 drop_insert_mode: str = "replace",
                 bulk_pragmas: bool = True):
        self.profile = profile
        self.layout = layout
        self.schema = table_schema or TableSchema.from_profile(profile)
        # 用 ``is None`` 而不是 ``or``：显式传 0 / 空串必须能被校验拦住，
        # 不能被当成「未指定」悄悄回落到默认值。
        self.batch_size = int(profile.retention.batch_size
                              if batch_size is None else batch_size)
        if self.batch_size <= 0:
            raise ValueError(f"batch_size 必须为正整数，当前为 {self.batch_size}")
        if drop_insert_mode not in ("replace", "plain", "upsert"):
            raise ValueError(f"drop_insert_mode 只能是 replace/plain/upsert，"
                             f"当前为 {drop_insert_mode!r}")
        self.drop_insert_mode = drop_insert_mode
        self.bulk_pragmas = bool(bulk_pragmas)
        # 只对分表 AVL 启用全局水位；固定表保持原有「单桶满批提交」语义，
        # 避免改变其它 AVL 与既有自检的行为。
        num_tables = int(getattr(layout, "num_tables", 0) or 0)
        if global_flush_rows is None:
            # 全局水位与 BATCH_SIZE 解耦：前者管「所有分桶合计多少行提交一次」，
            # 后者仍是「单个分桶满多少行立即提交」的原语义。
            self.global_flush_rows = (
                DEFAULT_GLOBAL_FLUSH_ROWS if num_tables > 1 else 0)
        else:
            self.global_flush_rows = int(global_flush_rows)
        if self.global_flush_rows < 0:
            raise ValueError("global_flush_rows 不能为负")
        self._drop_mode = self.drop_before_insert_enabled()
        self._sql_cache: dict[str, str] = {}
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
        conn = sqlite3.connect(str(db_path), timeout=30,
                               cached_statements=256 if self.bulk_pragmas else 128)
        self.stats.connections += 1
        cur = _configure(conn)
        if self.bulk_pragmas:
            # 大事务下默认 2MB page cache 会频繁把脏页刷回磁盘；
            # 64MB 缓存 + 内存临时表对千万级导入是纯收益，重启进程即失效。
            cur.execute("PRAGMA cache_size = -65536")
            cur.execute("PRAGMA temp_store = MEMORY")
            cur.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _sql_for(self, table: str) -> str:
        """按表名缓存 INSERT SQL，避免每批重新拼串 / prepare。"""
        sql = self._sql_cache.get(table)
        if sql is not None:
            return sql
        if self._drop_mode and self.drop_insert_mode != "upsert":
            names = ", ".join(self.schema.column_names)
            placeholders = ", ".join("?" for _ in self.schema.column_names)
            prefix = ("INSERT OR REPLACE" if self.drop_insert_mode == "replace"
                      else "INSERT")
            sql = f"{prefix} INTO {table} ({names}) VALUES ({placeholders})"
        else:
            sql = self.schema.insert_sql(table)
        self._sql_cache[table] = sql
        return sql

    def _flush(self, conn: sqlite3.Connection, table: str, batch: list) -> None:
        if not batch:
            return
        conn.executemany(self._sql_for(table), batch)
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

    def _make_router(self, layout, filename: str):
        """逐行路由：分表 AVL 走预生成表名的查表，其它布局回退原接口。"""
        num_tables = int(getattr(layout, "num_tables", 0) or 0)
        if (num_tables > 1 and hasattr(layout, "product_no_index")
                and hasattr(layout, "base") and hasattr(layout, "db_path")):
            from ..avl_specs import shard_index, table_name
            pidx = int(layout.product_no_index)
            db_key = str(Path(layout.db_path))
            names = [table_name(layout.base, i) for i in range(num_tables)]

            def route(row):
                return db_key, names[shard_index(row[pidx], num_tables)]
            return route

        def route(row):
            db_path, table = layout.target(filename, row)
            return str(Path(db_path)), table
        return route

    def write(self, filename: str, rows: Iterable[tuple], layout=None) -> int:
        """把解析器产出的记录按布局分桶、逐桶/全局满批提交，并登记批次水位。"""
        layout = self.layout if layout is None else layout
        self.stats = WriteStats()
        time_index = self._time_index()
        col_count = len(self.schema.columns)
        self._sql_cache.clear()

        # 全量替换模式：写入最新 AVL 之前先 DROP 并重建全部目标表，
        # 表里的历史数据（含上游不再下发的主键）一次性清干净。
        if self.drop_before_insert_enabled():
            self.stats.dropped_tables = self.drop_existing(layout)

        route = self._make_router(layout, filename)
        conns: dict[str, sqlite3.Connection] = {}
        batches: dict[tuple[str, str], list] = {}
        pending = 0

        def ensure_conn(db_key: str) -> sqlite3.Connection:
            conn = conns.get(db_key)
            if conn is None:
                Path(db_key).parent.mkdir(parents=True, exist_ok=True)
                conn = self._connect(Path(db_key))
                conns[db_key] = conn
            return conn

        def flush_bucket(key: tuple[str, str]) -> None:
            """原语义：单个分桶攒满 BATCH_SIZE，立即提交一次。"""
            nonlocal pending
            batch = batches.get(key)
            if not batch:
                return
            flushed = len(batch)
            self._flush(conns[key[0]], key[1], batch)
            batch.clear()
            pending -= flushed

        def flush_all() -> None:
            """新语义：全局累计达到阈值，同库所有桶一次事务提交。"""
            nonlocal pending
            by_db: dict[str, list[tuple[str, list]]] = {}
            for (db_key, table), batch in batches.items():
                if batch:
                    by_db.setdefault(db_key, []).append((table, batch))
            for db_key, items in by_db.items():
                conn = conns[db_key]
                for table, batch in items:
                    conn.executemany(self._sql_for(table), batch)
                    batch.clear()
                    self.stats.batches += 1
                conn.commit()
                self.stats.commits += 1
            pending = 0

        try:
            for row in rows:
                if type(row) is not tuple:
                    row = tuple(row)
                if len(row) != col_count:
                    raise ValueError(
                        f"记录列数 {len(row)} 与表结构 {col_count} 列不一致")
                if time_index >= 0:
                    op_time = str(row[time_index])
                    if op_time > self.stats.max_op_time:
                        self.stats.max_op_time = op_time
                db_key, table = route(row)
                ensure_conn(db_key)
                key = (db_key, table)
                bucket = batches.get(key)
                if bucket is None:
                    bucket = batches[key] = []
                bucket.append(row)
                self.stats.rows += 1
                pending += 1
                if len(bucket) >= self.batch_size:
                    flush_bucket(key)
                elif self.global_flush_rows and pending >= self.global_flush_rows:
                    flush_all()

            # 末尾余量：按库一次提交（原版是逐桶 128 次 commit）
            flush_all()
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

        # 业务数据已全部提交且没有异常：才推进水位。空批次（文件存在但
        # 0 条有效记录）不登记水位，避免把 latest_op_time 写成空串、让在线侧
        # 新鲜度窗口失去基准；控损的断供兜底也据此判定「文件有问题」。
        if self.stats.rows > 0:
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

    def roll_forward_op_time(self, days: int = 1,
                             tables: Iterable[str] | None = None) -> int:
        """控损断供兜底：把业务表里的 ``op_time`` 日期整体后移 ``days`` 天。

        只遍历 layout 枚举出的业务表，``_avl_meta`` 水位表保持不动（在线侧
        仍以原水位算窗口下界，后移后的数据只会更容易落在窗口内）。库文件
        不存在时直接跳过，避免为还没建库的 AVL 凭空造出空 db。返回实际
        更新的行数；单库失败记 ``stats.errors`` 并继续处理其余库。
        """
        days = int(days)
        if days <= 0:
            raise ValueError(f"days 必须为正整数，当前为 {days}")
        total = 0
        self.stats.rolled_rows = 0
        self.stats.errors = []

        groups = self._group_targets_by_db()
        if tables is not None:
            picked = set(tables)
            groups = {db: [t for t in ts if t in picked]
                      for db, ts in groups.items()}
            groups = {db: ts for db, ts in groups.items() if ts}

        for db_path, targets in groups.items():
            if not db_path.is_file():
                continue
            conn = None
            try:
                conn = self._connect(db_path)
                cur = conn.cursor()
                for t in targets:
                    cur.execute(self.schema.shift_time_sql(t, days))
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

        self.stats.rolled_rows = total
        return total


def build_sink(profile, *, layout=None, table_schema: TableSchema | None = None,
               batch_size: int | None = None,
               global_flush_rows: int | None = None,
               drop_insert_mode: str = "replace",
               bulk_pragmas: bool = True) -> SqliteSink:
    """装配写入层。"""
    if layout is None:
        from .router import build_layout
        layout = build_layout(profile)
    return SqliteSink(profile, layout, table_schema=table_schema,
                      batch_size=batch_size,
                      global_flush_rows=global_flush_rows,
                      drop_insert_mode=drop_insert_mode,
                      bulk_pragmas=bulk_pragmas)


__all__ = ["SqliteSink", "WriteStats", "build_sink"]
