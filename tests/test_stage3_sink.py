# -*- coding: utf-8 -*-
"""阶段 3 校验：Sink + Router（标准库 unittest，零新增依赖）。

覆盖：
1. 三条 SQL（DDL / INSERT / DELETE）与旧代码拼法**逐字相同**；
2. 端到端：4 个 ``io_samples/*.AVL`` 经新 parser + 新 sink 写入临时库后，
   与阶段 0 ``golden.json`` 的**全部 4 库 / 131 张表**逐字段比对
   （表名集合、``PRAGMA table_info`` 列定义、行数、rows_sha256、全量行内容）；
3. 路由与在线读取侧 ``services/data_query.py::_resolve()`` 一致；
4. ``INSERT OR REPLACE`` 覆盖主键、分桶批次边界、128 分表分布；
5. 过期清理与「库不存在 / 异常吞掉」语义；
6. 逐字保留的 ``synchronous`` 差异（商机 OFF / 控损 NORMAL）；
7. 零副作用：不加载 paramiko / loguru / portalocker，不写日志、不连网。

运行（仓库根目录）：
    python -m unittest discover -s tests -v
    SFTP_AVL2DB_GOLDEN=/path/to/golden.json python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crm_dbspec                                              # noqa: E402
from sftp_avl2db import TaskProfileConfig                      # noqa: E402
from sftp_avl2db.parsers import build_parser                   # noqa: E402
from sftp_avl2db.protocols import RecordSink, TableRouter      # noqa: E402
from sftp_avl2db.sink import (                                  # noqa: E402
    DEFAULT_SYNCHRONOUS, Crc32ShardLayout, FixedTableLayout,
    PerPrefixLayout, TableSchema, UnknownPrefixError,
    build_layout, build_sink,
)

GOLDEN = Path(os.environ.get("SFTP_AVL2DB_GOLDEN")
              or ROOT.parent.parent / "baseline" / "golden.json")
SAMPLES = ROOT / "io_samples"

FAKE_SFTP = {"host": "h", "port": 22, "username": "u",
             "password": "p", "remote_path": "/r"}


def _profile(runtime, name, routing, num_tables=0, table_prefix="",
             batch_size=None):
    """用纯 dict 造 profile，不读 .env（与阶段 1 ``from_mapping`` 一致）。"""
    d = {
        "name": name,
        "file_prefixes": (["F09554", "F09555", "F09556"]
                          if name == "opportunity_recall" else ["F09591"]),
        "sftp": FAKE_SFTP,
        "runtime_dir": str(runtime),
        "schema": crm_dbspec.DB_SCHEMAS[name],
        "parser": "flat" if name == "opportunity_recall" else "continuation",
        "routing": routing,
        "num_tables": num_tables,
        "table_name_prefix": table_prefix,
    }
    if batch_size:
        d["batch_size"] = batch_size
    return TaskProfileConfig.from_mapping(d)


def _norm_rows(rows):
    """与 baseline/make_baseline.py::_norm_rows 完全一致。"""
    out = [list(r) for r in rows]
    out.sort(key=lambda r: tuple("" if v is None else str(v) for v in r))
    return out


def _dump_db(db: Path) -> dict:
    """与 baseline/make_baseline.py::_dump_db 同口径导出一张库的全部表。"""
    conn = sqlite3.connect(str(db))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result = {}
        for t in tables:
            info = [list(r) for r in
                    [(x[1], x[2], x[5])
                     for x in conn.execute(f'PRAGMA table_info("{t}")')]]
            rows = _norm_rows(conn.execute(f'SELECT * FROM "{t}"').fetchall())
            result[t] = {
                "columns": info,
                "row_count": len(rows),
                "rows_sha256": hashlib.sha256(
                    json.dumps(rows, ensure_ascii=False, sort_keys=True,
                               default=str).encode("utf-8")
                ).hexdigest(),
                "rows": rows,
            }
        return {"tables": result}
    finally:
        conn.close()


def _dump_all(root: Path) -> dict:
    return {db.name: _dump_db(db) for db in sorted(root.rglob("*.db"))}


def _golden_by_db_name(golden) -> dict:
    return {Path(k).name: v for k, v in golden["databases"].items()}


def _row(n: int, op_time="2026-11-15") -> tuple:
    """造一条商机 12 列记录，``product_no`` 唯一。"""
    return (op_time, "2026-11-06", f"1390000{n:04d}", "大类", "小类",
            "摘要", "需求", "策略", "无", "备注", "参考", 3)


def _cont_row(p_no: str, op_time="2026-11-15") -> tuple:
    return (op_time, "u1", p_no, "11", "策略正文")


class SchemaSqlTests(unittest.TestCase):
    """旧代码拼法逐字复刻 → 与 TableSchema 输出必须完全相等。"""

    def _legacy_sql(self, schema):
        cols = ", ".join(f"{c} {t}" for c, t in schema)
        names = [c[0] for c in schema]
        return {
            "ddl": f"CREATE TABLE IF NOT EXISTS T ({cols}, PRIMARY KEY (product_no))",
            "insert": (f"INSERT OR REPLACE INTO T ({', '.join(names)}) "
                       f"VALUES ({', '.join('?' for _ in names)})"),
            "delete": "DELETE FROM T WHERE op_time < ?",
        }

    def test_sql_text_matches_legacy_verbatim(self):
        for name, schema in crm_dbspec.DB_SCHEMAS.items():
            with self.subTest(task=name):
                ts = TableSchema(tuple(tuple(c) for c in schema))
                legacy = self._legacy_sql(schema)
                self.assertEqual(ts.create_table_sql("T"), legacy["ddl"])
                self.assertEqual(ts.insert_sql("T"), legacy["insert"])
                self.assertEqual(ts.delete_expired_sql("T"), legacy["delete"])
                self.assertEqual(ts.column_ddl(),
                                 ", ".join(f"{c} {t}" for c, t in schema))
                self.assertEqual(ts.column_names, [c[0] for c in schema])

    def test_validate_row(self):
        ts = TableSchema(tuple(tuple(c) for c in
                               crm_dbspec.DB_SCHEMAS["loss_control"]))
        self.assertIsNone(ts.validate_row(_cont_row("138")))
        with self.assertRaises(ValueError):
            ts.validate_row(("only", "two"))

    def test_from_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "opportunity_recall", "per_prefix")
            self.assertEqual(TableSchema.from_profile(p).column_names,
                             p.columns)


@unittest.skipUnless(SAMPLES.is_dir(), f"缺少样本目录 {SAMPLES}")
class GoldenFullComparisonTests(unittest.TestCase):
    """端到端：新 parser + 新 sink → 临时库，与 golden 全量 131 表比对。"""

    @classmethod
    def setUpClass(cls):
        if not GOLDEN.is_file():
            raise unittest.SkipTest(
                f"缺少 golden 基线 {GOLDEN}；可用 SFTP_AVL2DB_GOLDEN 指定路径")
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def _run_all(self, root: Path):
        specs = (
            ("opportunity_recall", "per_prefix", 0, ""),
            ("loss_control", "crc32_shards", 128, "data_F09591"),
        )
        for name, routing, n, prefix in specs:
            profile = _profile(root, name, routing, n, prefix)
            layout = build_layout(profile)
            sink = build_sink(profile, layout=layout)
            sink.ensure_schema()
            parser = build_parser(profile)
            for meta in self.golden["source_files"].values():
                if meta["task"] != name:
                    continue
                fn = next(k for k, v in self.golden["source_files"].items()
                          if v is meta)
                sink.write(fn, parser.parse(SAMPLES / fn), router=layout)

    def test_all_131_tables_match_golden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run_all(root)
            got = _dump_all(root / "sqlite_db")
            want = _golden_by_db_name(self.golden)

            self.assertEqual(set(got), set(want),
                             f"库文件集合不一致: got={sorted(got)} want={sorted(want)}")
            total_tables = 0
            for db_name, wdb in want.items():
                with self.subTest(db=db_name):
                    gdb = got[db_name]
                    self.assertEqual(set(gdb["tables"]), set(wdb["tables"]),
                                     f"{db_name} 表名集合不一致")
                    for table, wtv in wdb["tables"].items():
                        gtv = gdb["tables"][table]
                        total_tables += 1
                        self.assertEqual(gtv["columns"], wtv["columns"],
                                         f"{db_name}.{table} 列定义不一致")
                        self.assertEqual(gtv["row_count"], wtv["row_count"],
                                         f"{db_name}.{table} 行数不一致")
                        self.assertEqual(gtv["rows_sha256"], wtv["rows_sha256"],
                                         f"{db_name}.{table} 内容 sha256 不一致")
                        self.assertEqual(gtv["rows"], wtv["rows"],
                                         f"{db_name}.{table} 全量行内容不一致")
            self.assertEqual(total_tables, 131, "应比对全部 131 张表")

    def test_write_counts_and_batch_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = _profile(root, "loss_control", "crc32_shards",
                               128, "data_F09591")
            layout = build_layout(profile)
            sink = build_sink(profile, layout=layout)
            sink.ensure_schema()
            fn = "F0959120261110000000.AVL"
            parser = build_parser(profile)
            n = sink.write(fn, parser.parse(SAMPLES / fn), router=layout)
            self.assertEqual(n, 2)
            self.assertEqual(sink.stats.rows, 2)
            self.assertEqual(sink.stats.batches, 1)   # 末尾余量一次提交
            self.assertEqual(sink.stats.commits, 1)
            self.assertEqual(sink.stats.connections, 1)  # 单库只连一次

    def test_shard_36_matches_read_side_resolve(self):
        """写入路由与 services/data_query.py::_resolve() 的库表口径一致。"""
        import services.data_query as dq

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = _profile(root, "loss_control", "crc32_shards",
                               128, "data_F09591")
            layout = build_layout(profile)
            p_no = "17854339544"

            # 阶段 5 后读取侧从 PROFILES[task_name].db_dir 取路径，
            # 直接注入本次构造的 profile 即可，无需真实 .env 库目录。
            with mock.patch.object(dq, "PROFILES", {"loss_control": profile}):
                resolved = dq._resolve("loss_control", p_no, None)
            self.assertIsNotNone(resolved)
            db_path, table, values = resolved

            self.assertEqual(Path(layout.db_path).name, Path(db_path).name)
            self.assertEqual(layout.target("F09591xxx.AVL", _cont_row(p_no))[1],
                             table)
            self.assertEqual(values, [p_no])

    def test_per_prefix_matches_read_side_resolve(self):
        import services.data_query as dq

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = _profile(root, "opportunity_recall", "per_prefix")
            layout = build_layout(profile)
            product_id = "202608041615335173"     # → F09554

            with mock.patch.object(dq, "PROFILES",
                                   {"opportunity_recall": profile}):
                resolved = dq._resolve("opportunity_recall", "13900000001",
                                       product_id)
            self.assertIsNotNone(resolved)
            db_path, table, _ = resolved
            self.assertEqual(Path(layout.target(
                "F0955420261110000000.AVL", _row(1))[0]).name,
                Path(db_path).name)
            self.assertEqual(layout.target(
                "F0955420261110000000.AVL", _row(1))[1], table)


class WriteBehaviorTests(unittest.TestCase):

    def test_insert_or_replace_collapses_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = _profile(tmp, "opportunity_recall", "per_prefix")
            layout, sink = build_layout(profile), None
            sink = build_sink(profile, layout=layout)
            sink.ensure_schema()
            old = _row(1, "2026-11-01")
            new = _row(1, "2026-11-15")
            n = sink.write("F0955420261110000000.AVL", [old, new], router=layout)
            self.assertEqual(n, 2)                       # 写入 2 条
            dump = _dump_all(profile.db_dir)["F09554.db"]
            tv = dump["tables"]["data_F09554"]
            self.assertEqual(tv["row_count"], 1)        # 覆盖后只剩 1 行
            self.assertEqual(tv["rows"][0][0], "2026-11-15")  # 后写覆盖先写

    def test_batch_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = _profile(tmp, "opportunity_recall", "per_prefix",
                               batch_size=2)
            layout = build_layout(profile)
            sink = build_sink(profile, layout=layout)
            sink.ensure_schema()
            rows = [_row(i) for i in range(5)]
            n = sink.write("F0955420261110000000.AVL", rows, router=layout)
            self.assertEqual(n, 5)
            self.assertEqual(sink.stats.batches, 3)     # 2 + 2 + 1
            self.assertEqual(sink.stats.commits, 3)
            self.assertEqual(sink.stats.connections, 1)
            self.assertEqual(_dump_all(profile.db_dir)["F09554.db"]
                             ["tables"]["data_F09554"]["row_count"], 5)

    def test_stats_reset_between_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = _profile(tmp, "opportunity_recall", "per_prefix",
                               batch_size=2)
            layout = build_layout(profile)
            sink = build_sink(profile, layout=layout)
            sink.ensure_schema()
            sink.write("F0955420261110000000.AVL",
                       [_row(i) for i in range(3)], router=layout)
            self.assertEqual(sink.stats.batches, 2)
            sink.write("F0955520261110000000.AVL",
                       [_row(i) for i in range(10, 13)], router=layout)
            self.assertEqual(sink.stats.rows, 3)
            self.assertEqual(sink.stats.batches, 2)     # 不累加

    def test_shard_distribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = _profile(tmp, "loss_control", "crc32_shards",
                               8, "data_X", batch_size=7)
            layout = build_layout(profile)
            sink = build_sink(profile, layout=layout)
            sink.ensure_schema()
            p_nos = [f"1380000{i:04d}" for i in range(200)]
            n = sink.write("F0959120261110000000.AVL",
                           [_cont_row(p) for p in p_nos], router=layout)
            self.assertEqual(n, 200)
            dump = _dump_all(profile.db_dir)["F09591.db"]["tables"]
            self.assertEqual(sum(v["row_count"] for v in dump.values()), 200)
            self.assertEqual(len([v for v in dump.values() if v["row_count"]]), 8)
            for p in p_nos:
                idx = zlib.crc32(p.encode("utf-8")) % 8
                table = f"data_X_{idx}"
                rows = dump[table]["rows"]
                self.assertTrue(any(r[2] == p for r in rows),
                                f"{p} 应落在 {table}")
            self.assertGreater(sink.stats.batches, 8)   # 满 7 条即按桶提交

    def test_unknown_prefix_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = PerPrefixLayout(Path(tmp), ["F09554"])
            with self.assertRaises(UnknownPrefixError):
                layout.target("UNKNOWN20261110000000.AVL", _row(1))

    def test_fixed_table_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = _profile(tmp, "loss_control", "fixed_table")
            layout = build_layout(profile, table_name="data_fixed")
            self.assertIsInstance(layout, FixedTableLayout)
            self.assertEqual(list(layout.all_targets()),
                             [(profile.db_dir / "F09591.db", "data_fixed")])
            self.assertEqual(layout.target("any.AVL", _cont_row("1"))[1],
                             "data_fixed")
            sink = build_sink(profile, layout=layout)
            sink.ensure_schema()
            sink.write("any.AVL", [_cont_row("1")], router=layout)
            self.assertEqual(_dump_all(profile.db_dir)["F09591.db"]
                             ["tables"]["data_fixed"]["row_count"], 1)

    def test_bad_routing_and_bad_shard_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                build_layout(_profile(tmp, "loss_control", "unknown"))
            with self.assertRaises(ValueError):
                build_layout(_profile(tmp, "loss_control", "crc32_shards",
                                      0, "data_X"))
            with self.assertRaises(ValueError):
                Crc32ShardLayout(Path(tmp), [], 8, "data_X")


class PurgeTests(unittest.TestCase):

    def test_purge_expired_deletes_only_old_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = _profile(tmp, "opportunity_recall", "per_prefix")
            layout = build_layout(profile)
            sink = build_sink(profile, layout=layout)
            sink.ensure_schema()
            rows = [_row(i, "2026-11-01") for i in range(3)] + \
                   [_row(i, "2026-11-15") for i in range(3, 6)]
            sink.write("F0955420261110000000.AVL", rows, router=layout)
            deleted = sink.purge_expired("2026-11-10")
            self.assertEqual(deleted, 3)
            self.assertEqual(sink.stats.purged, 3)
            self.assertEqual(sink.stats.errors, [])
            left = _dump_all(profile.db_dir)["F09554.db"]["tables"]["data_F09554"]
            self.assertEqual(left["row_count"], 3)
            self.assertTrue(all(r[0] == "2026-11-15" for r in left["rows"]))

    def test_purge_expired_on_all_128_shard_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = _profile(tmp, "loss_control", "crc32_shards",
                               8, "data_X")
            layout = build_layout(profile)
            sink = build_sink(profile, layout=layout)
            sink.ensure_schema()
            sink.write("any.AVL",
                       [_cont_row(f"138{i:04d}", "2026-11-01") for i in range(20)],
                       router=layout)
            self.assertEqual(sink.purge_expired("2026-11-10"), 20)
            dump = _dump_all(profile.db_dir)["F09591.db"]["tables"]
            self.assertEqual(sum(v["row_count"] for v in dump.values()), 0)

    def test_purge_expired_missing_db_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = _profile(tmp, "opportunity_recall", "per_prefix")
            sink = build_sink(profile)
            self.assertEqual(sink.purge_expired("2026-11-10"), 0)
            self.assertEqual(sink.stats.errors, [])


class PolicyAndProtocolTests(unittest.TestCase):

    def test_synchronous_difference_is_preserved(self):
        """商机 OFF / 控损 NORMAL 是现状差异，刻意不统一。"""
        self.assertEqual(DEFAULT_SYNCHRONOUS["per_prefix"], "OFF")
        self.assertEqual(DEFAULT_SYNCHRONOUS["crc32_shards"], "NORMAL")
        self.assertEqual(DEFAULT_SYNCHRONOUS["fixed_table"], "NORMAL")
        with tempfile.TemporaryDirectory() as tmp:
            p1 = _profile(tmp, "opportunity_recall", "per_prefix")
            p2 = _profile(tmp, "loss_control", "crc32_shards", 8, "data_X")
            self.assertEqual(build_sink(p1).synchronous, "OFF")
            self.assertEqual(build_sink(p2).synchronous, "NORMAL")
            self.assertEqual(build_sink(p1, synchronous="FULL").synchronous,
                             "FULL")     # 允许显式覆盖

    def test_protocols_are_satisfied(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = _profile(tmp, "loss_control", "crc32_shards",
                               8, "data_X")
            layout = build_layout(profile)
            sink = build_sink(profile, layout=layout)
            self.assertIsInstance(layout, TableRouter)
            self.assertIsInstance(sink, RecordSink)

    def test_bad_batch_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = _profile(tmp, "opportunity_recall", "per_prefix")
            with self.assertRaises(ValueError):
                build_sink(profile, batch_size=0)

    def test_sink_sources_have_no_third_party_io(self):
        banned = ("import paramiko", "import loguru", "import portalocker",
                  "logger.", "socket.", "requests.")
        for p in sorted((ROOT / "sftp_avl2db" / "sink").glob("*.py")):
            src = p.read_text(encoding="utf-8")
            for word in banned:
                self.assertNotIn(word, src, f"{p.name} 不应包含 {word}")

    def test_subprocess_loads_no_third_party_modules(self):
        """无 .env 的子进程端到端写临时库，不加载 paramiko/loguru/portalocker。"""
        code = r'''
import sys, os, tempfile
from pathlib import Path
repo, samples = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
os.environ.clear()

from sftp_avl2db import TaskProfileConfig
from sftp_avl2db.parsers import build_parser
from sftp_avl2db.sink import build_layout, build_sink
import crm_dbspec

with tempfile.TemporaryDirectory() as tmp:
    for name, routing, n, prefix in (
        ("opportunity_recall", "per_prefix", 0, ""),
        ("loss_control", "crc32_shards", 128, "data_F09591"),
    ):
        p = TaskProfileConfig.from_mapping({
            "name": name,
            "file_prefixes": (["F09554","F09555","F09556"]
                              if name == "opportunity_recall" else ["F09591"]),
            "sftp": {"host":"h","port":22,"username":"u","password":"p",
                     "remote_path":"/r"},
            "runtime_dir": tmp,
            "schema": crm_dbspec.DB_SCHEMAS[name],
            "parser": "flat" if name == "opportunity_recall" else "continuation",
            "routing": routing, "num_tables": n,
            "table_name_prefix": prefix,
        })
        layout = build_layout(p)
        sink = build_sink(p, layout=layout)
        sink.ensure_schema()
        for f in sorted(Path(samples).glob("*.AVL")):
            if any(f.name.startswith(x) for x in p.file_prefixes):
                sink.write(f.name, build_parser(p).parse(f), router=layout)

banned = ("paramiko", "loguru", "portalocker")
loaded = [m for m in banned if m in sys.modules]
if loaded:
    raise AssertionError("sink 层加载了第三方模块: " + repr(loaded))
print("SINK_PURE")
'''
        r = subprocess.run([sys.executable, "-c", code, str(ROOT), str(SAMPLES)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0,
                         f"stderr:\n{r.stderr}\nstdout:\n{r.stdout}")
        self.assertIn("SINK_PURE", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
