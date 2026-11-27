"""连接生命周期 ``_session()`` 契约自检（行为等价，零第三方依赖）。

不打任何服务、不连 SFTP。覆盖：

1. ``bulk`` 两种口径：非 bulk 只设 WAL + synchronous=NORMAL 且不计数；
   bulk 额外设 cache_size / temp_store / busy_timeout 且累计 connections；
2. 异常由 ``_session`` 统一 rollback 后原样抛出，未提交写入不残留；
3. 两类错误策略保持不变：``purge_expired`` / ``roll_forward_op_time`` /
   ``_cleanup_old`` 记 ``stats.errors`` 后不抛；``ensure_schema`` /
   ``drop_existing`` / ``_build_stage_indexes`` 直接抛；
4. 改造方法的正常返回值与 ``WriteStats`` 关键字段不变。

用法：``python3 tests/check_db_session.py``
"""
from __future__ import annotations

import sqlite3 as sqlite3_mod
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sftp_avl2db.config import load_profiles                 # noqa: E402
from sftp_avl2db.sink import build_sink                     # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="db_session_"))
_failures = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' +
          (f" → {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


def sink_for(avl, tag, **kw):
    p = load_profiles(runtime_dir=str(TMP / tag))[avl]
    return p, build_sink(p, **kw)


def capture_sql(fn):
    """记录本次调用期间各 SQLite 连接实际执行的语句（含 PRAGMA）。"""
    calls = []
    orig = sqlite3_mod.connect

    def spy(*args, **kwargs):
        conn = orig(*args, **kwargs)
        conn.set_trace_callback(calls.append)
        return conn

    sqlite3_mod.connect = spy
    try:
        fn()
    finally:
        sqlite3_mod.connect = orig
    return list(calls)


def pragmas(sqls):
    return [s for s in sqls if s.upper().startswith("PRAGMA")]


class BrokenSchema:
    """包住真实 schema，只让指定的 SQL 生成方法返回非法语句。"""

    def __init__(self, inner, broken):
        self._inner = inner
        self._broken = set(broken)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def create_table_sql(self, t):
        return ("THIS IS NOT SQL" if "create" in self._broken
                else self._inner.create_table_sql(t))

    def delete_expired_sql(self, t):
        return ("THIS IS NOT SQL" if "delete" in self._broken
                else self._inner.delete_expired_sql(t))

    def shift_time_sql(self, t, days):
        return ("THIS IS NOT SQL" if "shift" in self._broken
                else self._inner.shift_time_sql(t, days))


def rows_of(db_path, table):
    conn = sqlite3_mod.connect(str(db_path))
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


def main():
    print("=== 1. bulk / 非 bulk 的 PRAGMA 与连接计数 ===")
    p, s_plain = sink_for("F09627", "pragma_plain")
    db = p.db_dir / "probe.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    def open_plain():
        with s_plain._session(db) as (conn, _):
            conn.commit()

    plain_sql = pragmas(capture_sql(open_plain))
    check("非 bulk 只设 WAL + synchronous=NORMAL",
          len(plain_sql) == 2
          and any("journal_mode" in x for x in plain_sql)
          and any("synchronous" in x for x in plain_sql), str(plain_sql))
    check("非 bulk 不累计 stats.connections",
          s_plain.stats.connections == 0, s_plain.stats.connections)

    _, s_bulk = sink_for("F09627", "pragma_bulk")

    def open_bulk():
        with s_bulk._session(db, bulk=True) as (conn, _):
            conn.commit()

    bulk_sql = pragmas(capture_sql(open_bulk))
    check("bulk 额外设 cache_size / temp_store / busy_timeout",
          len(bulk_sql) == 5
          and any("cache_size" in x for x in bulk_sql)
          and any("temp_store" in x for x in bulk_sql)
          and any("busy_timeout" in x for x in bulk_sql), str(bulk_sql))
    check("bulk 累计 stats.connections",
          s_bulk.stats.connections == 1, s_bulk.stats.connections)

    print("=== 2. 异常统一 rollback 后原样抛出 ===")
    p2, s2 = sink_for("F09627", "rollback")
    db2 = p2.db_dir / "probe.db"
    db2.parent.mkdir(parents=True, exist_ok=True)
    caught = []

    def boom():
        with s2._session(db2) as (conn, cur):
            # sqlite3 默认对 DDL 不自动开事务，显式 BEGIN 才能验证 rollback。
            conn.execute("BEGIN")
            cur.execute("CREATE TABLE should_vanish (x INTEGER)")
            raise RuntimeError("boom")

    try:
        boom()
    except RuntimeError as e:
        caught.append(str(e))
    check("异常原样抛出", caught == ["boom"], str(caught))
    conn = sqlite3_mod.connect(str(db2))
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='should_vanish'"
        ).fetchone()[0]
    finally:
        conn.close()
    check("未提交的 DDL 已回滚（无残留表）", left == 0, left)

    print("=== 3. 两类错误策略不变 ===")
    # best-effort: purge_expired / roll_forward_op_time
    p3, s3 = sink_for("F09627", "best_effort")
    s3.ensure_schema()
    s3.write("F0962720261120000000.AVL",
             [("2026-11-20", "超套", 1), ("2026-11-20", "流量", 2)])
    real_schema = s3.schema
    s3.schema = BrokenSchema(real_schema, {"delete"})
    r_purge = s3.purge_expired("2027-01-01")
    check("purge_expired 失败返回 0 且记 errors，不抛",
          r_purge == 0 and len(s3.stats.errors) == 1
          and "F09627.db" in s3.stats.errors[0][0],
          f"{r_purge} / {s3.stats.errors}")
    s3.schema = BrokenSchema(real_schema, {"shift"})
    s3.stats.errors = []
    r_roll = s3.roll_forward_op_time(days=1)
    check("roll_forward_op_time 失败返回 0 且记 errors，不抛",
          r_roll == 0 and len(s3.stats.errors) == 1,
          f"{r_roll} / {s3.stats.errors}")
    s3.schema = real_schema

    # best-effort: _cleanup_old（注入非法表名让 DROP 失败）
    _, s33 = sink_for("F09633", "cleanup", atomic_swap=True)
    s33.ensure_schema()
    s33._table_names = lambda: (["cur"], ["stg"], ["bad-name"])
    n_clean = s33._cleanup_old()
    check("_cleanup_old 失败记 errors 后不抛",
          n_clean is None and len(s33.stats.errors) == 1
          and s33.stats.errors[0][1].startswith("cleanup_old:"),
          str(s33.stats.errors))

    # fail-fast: ensure_schema / drop_existing / _build_stage_indexes
    _, s4 = sink_for("F09627", "fail_fast")
    s4.schema = BrokenSchema(s4.schema, {"create"})
    check("ensure_schema 失败直接抛", raises(lambda: s4.ensure_schema()))
    check("drop_existing 失败直接抛", raises(lambda: s4.drop_existing()))

    _, s5 = sink_for("F09633", "stage_idx", atomic_swap=True)
    # 未跑 _prepare_stage()，stage 表不存在 → CREATE INDEX 失败
    check("_build_stage_indexes 失败直接抛",
          raises(lambda: s5._build_stage_indexes()))
    check("失败后 stage_indexes 未被写入",
          s5.stats.stage_indexes == 0, s5.stats.stage_indexes)

    print("=== 4. 正常路径返回值与关键 stats ===")
    p6, s6 = sink_for("F09627", "normal")
    s6.ensure_schema()
    n = s6.write("F0962720261120000000.AVL",
                 [("2026-11-20", "超套", 1),
                  ("2026-11-20", "流量", 2),
                  ("2026-11-20", "资费", 3)])
    check("write 返回 3 且表内 3 行",
          n == 3 and len(rows_of(p6.db_dir / "F09627.db", "F09627")) == 3,
          str(n))
    check("_avl_meta 水位已登记",
          len(rows_of(p6.db_dir / "F09627.db", "_avl_meta")) == 1)

    rolled = s6.roll_forward_op_time(days=1)
    check("roll_forward_op_time 返回 3 且 stats.rolled_rows=3",
          rolled == 3 and s6.stats.rolled_rows == 3, str(rolled))
    check("op_time 已整体 +1 天",
          all(r[0] == "2026-11-21"
              for r in rows_of(p6.db_dir / "F09627.db", "F09627")),
          str(rows_of(p6.db_dir / "F09627.db", "F09627")))
    purged = s6.purge_expired("2027-01-01")
    check("purge_expired 返回 3 且 stats.purged=3",
          purged == 3 and s6.stats.purged == 3, str(purged))
    dropped = s6.drop_existing()
    check("drop_existing 返回 1 且表已清空",
          dropped == 1 and s6.stats.dropped_tables == 1
          and rows_of(p6.db_dir / "F09627.db", "F09627") == [],
          str(dropped))
    check("drop_existing 后 _avl_meta 仍保留",
          len(rows_of(p6.db_dir / "F09627.db", "_avl_meta")) == 1)

    # F09633 原子切换：9 个方法里剩下的 4 个由完整流程覆盖
    p7, s7 = sink_for("F09633", "swap", atomic_swap=True)
    s7.ensure_schema()
    rows = [("2026-11-20", "u%d" % i, "1380000%04d" % i, "超套", "痛点%d" % i)
            for i in range(12)]
    n2 = s7.write("F0963320261120000000.AVL", rows)
    conn = sqlite3_mod.connect(str(p7.db_dir / "F09633.db"))
    try:
        idx_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'idx_F09633%'").fetchone()[0]
        swap_left = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND (name LIKE 'F09633_stage_%' OR name LIKE 'F09633_old_%')"
        ).fetchone()[0]
        meta = conn.execute(
            "SELECT latest_file_date FROM _avl_meta WHERE avl='F09633'"
        ).fetchone()
    finally:
        conn.close()
    check("原子切换写入 12 行", n2 == 12 and s7.stats.rows == 12, str(n2))
    check("128 张 stage 索引已建且 stats.stage_indexes=128",
          idx_count == 128 and s7.stats.stage_indexes == 128,
          f"{idx_count}/{s7.stats.stage_indexes}")
    check("切换统计与水位",
          s7.stats.swapped_tables == 128 and meta == ("20261120",)
          and swap_left == 0,
          f"{s7.stats.swapped_tables}/{meta}/{swap_left}")
    check("切换事务耗时已记录",
          s7.stats.swap_seconds >= 0, s7.stats.swap_seconds)
    check("整个流程无 errors", s7.stats.errors == [], str(s7.stats.errors))

    print("=== 结论 ===")
    if _failures:
        print("FAILED: %d 项 → %s" % (len(_failures), _failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
