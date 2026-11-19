# -*- coding: utf-8 -*-
"""阶段 2 校验：纯解析层（标准库 unittest，零新增依赖）。

覆盖：
1. 4 个 ``io_samples/*.AVL`` 的解析输出，与阶段 0 ``golden.json`` 的
   ``databases.*.rows`` 逐字段一致（按主键模拟 INSERT OR REPLACE 覆盖后比对）；
2. 解析条数 / 跳过数与 ``golden.json`` 的 ``log_signals`` 对照；
3. 控损分表号与 ``crc32(product_no) % 128`` 口径一致（样本落在 ``_36``）；
4. 合成用例：列数不足 / 超出、非数字与空 city_id、空主键、strip 行为、
   编码探测保真、续行重组、只剥一个反斜杠、4 列起始行、<4 列损坏行、
   文件开头孤立续行、空物理行、最后一条单独产出、>=5 列丢弃尾列；
5. 零副作用实测：解析层源码不引入 paramiko / sqlite3 / loguru，
   子进程里跑真实样本不加载这些模块，且打桩确认不建库 / 不连网。

运行（仓库根目录）：
    python -m unittest discover -s tests -v
    SFTP_AVL2DB_GOLDEN=/path/to/golden.json python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
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

import crm_dbspec                                             # noqa: E402
from sftp_avl2db.parsers import (                             # noqa: E402
    ENCODING_CANDIDATES, FlatColumnParser, ContinuationParser,
    build_parser, split_head, strip_eol,
)
from sftp_avl2db.protocols import RecordParser                 # noqa: E402

GOLDEN = Path(os.environ.get("SFTP_AVL2DB_GOLDEN")
              or ROOT.parent.parent / "baseline" / "golden.json")
SAMPLES = ROOT / "io_samples"


class _Profile:
    """给 ``build_parser`` 用的最小 profile（避免阶段 2 测试依赖 .env）。"""

    def __init__(self, name: str, parser_kind: str):
        self.name = name
        self.parser = parser_kind
        self.columns = [c[0] for c in crm_dbspec.DB_SCHEMAS[name]]


PROFILES = {
    "opportunity_recall": _Profile("opportunity_recall", "flat"),
    "loss_control": _Profile("loss_control", "continuation"),
}


def _norm_rows(rows):
    """与 baseline/make_baseline.py::_norm_rows 完全一致的排序口径。"""
    out = [list(r) for r in rows]
    out.sort(key=lambda r: tuple("" if v is None else str(v) for v in r))
    return out


def _collapse_by_pk(rows, pk_index):
    """模拟 ``PRIMARY KEY`` + ``INSERT OR REPLACE``：后写覆盖先写。"""
    kept = {}
    for r in rows:
        kept[str(r[pk_index])] = list(r)
    return _norm_rows(kept.values())


def _parse_text(parser, text: str, encoding: str = "utf-8"):
    """把字符串当 AVL 文件喂给解析器，返回 (rows, stats)。"""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "sample.AVL"
        p.write_bytes(text.encode(encoding))
        rows = list(parser.parse(p))
        return rows, parser.stats


def _flat(text, column_count=12, encoding="utf-8"):
    return _parse_text(FlatColumnParser(column_count), text, encoding)


def _cont(text, encoding="utf-8"):
    return _parse_text(ContinuationParser(), text, encoding)


@unittest.skipUnless(SAMPLES.is_dir(), f"缺少样本目录 {SAMPLES}")
class GoldenComparisonTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not GOLDEN.is_file():
            raise unittest.SkipTest(
                f"缺少 golden 基线 {GOLDEN}；可用 SFTP_AVL2DB_GOLDEN 指定路径")
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def _nonempty_table(self, prefix):
        """按 .db 文件名前缀找到 golden 里唯一有数据的表。"""
        keys = [k for k in self.golden["databases"] if f"/{prefix}.db" in k]
        self.assertEqual(len(keys), 1, f"{prefix} 应对应 1 个库，实际 {keys}")
        tables = self.golden["databases"][keys[0]]["tables"]
        nonempty = {t: v for t, v in tables.items() if v["row_count"]}
        self.assertEqual(len(nonempty), 1,
                         f"{prefix} 应只有 1 张非空表，实际 {list(nonempty)}")
        return keys[0], next(iter(nonempty.items()))

    def test_parser_output_matches_golden_rows(self):
        """4 个样本：解析输出经主键覆盖后，与 golden 表内容逐字段相等。"""
        for name in self.golden["source_files"]:
            with self.subTest(file=name):
                meta = self.golden["source_files"][name]
                parser = build_parser(PROFILES[meta["task"]])
                rows = list(parser.parse(SAMPLES / name))

                _, (table_name, tv) = self._nonempty_table(meta["prefix"])
                collapsed = _collapse_by_pk(rows, pk_index=2)
                self.assertEqual(collapsed, tv["rows"],
                                 f"{name} 解析输出与 golden.{table_name}.rows 不一致")

                # 列数 / 列名顺序也要与建表契约一致
                self.assertEqual(len(rows[0]), len(PROFILES[meta["task"]].columns))
                self.assertEqual([c[0] for c in tv["columns"]],
                                 PROFILES[meta["task"]].columns)

    def test_parse_counts_match_log_signals(self):
        """解析条数 / 跳过数与 golden 记录的现有日志信号一致。"""
        for task_name, sig in self.golden["log_signals"].items():
            pairs = list(zip(sig["files"], sig["total_seen"], sig["skipped_seen"]))
            for fname, exp_rows, exp_skipped in pairs:
                with self.subTest(file=fname):
                    parser = build_parser(PROFILES[task_name])
                    rows = list(parser.parse(SAMPLES / fname))
                    self.assertEqual(parser.stats.rows, exp_rows)
                    self.assertEqual(len(rows), exp_rows)
                    self.assertEqual(parser.stats.skipped, exp_skipped)

    def test_loss_control_shard_matches_crc32(self):
        """控损样本应落在 crc32(product_no) % 128 对应的分表。"""
        name = "F0959120261110000000.AVL"
        parser = ContinuationParser()
        rows = list(parser.parse(SAMPLES / name))
        _, (table_name, _) = self._nonempty_table("F09591")
        for r in rows:
            idx = zlib.crc32(str(r[2]).encode("utf-8")) % 128
            self.assertTrue(table_name.endswith(f"_{idx}"),
                            f"{table_name} 与 crc32={idx} 不符")
        self.assertEqual(table_name, "data_F09591_36")

    def test_sample_physical_line_counts(self):
        """样本物理行数与 golden 记录一致，防止样本被误改。"""
        for name, meta in self.golden["source_files"].items():
            with self.subTest(file=name):
                raw = (SAMPLES / name).read_bytes()
                self.assertEqual(len(raw), meta["bytes"])
                self.assertEqual(len(raw.splitlines()), meta["physical_lines"])

    def test_parser_implements_protocol(self):
        """两个实现都满足 RecordParser 协议（含 stats 数据成员）。"""
        self.assertIsInstance(FlatColumnParser(12), RecordParser)
        self.assertIsInstance(ContinuationParser(), RecordParser)
        self.assertIsInstance(build_parser(PROFILES["loss_control"]),
                              ContinuationParser)
        self.assertIsInstance(build_parser(PROFILES["opportunity_recall"]),
                              FlatColumnParser)
        with self.assertRaises(ValueError):
            build_parser(_Profile("loss_control", "unknown"))


@unittest.skipUnless(SAMPLES.is_dir(), f"缺少样本目录 {SAMPLES}")
class ZeroSideEffectTests(unittest.TestCase):

    def test_parser_sources_have_no_io_imports(self):
        """解析层源码里不应出现网络 / DB / 日志依赖。"""
        parser_dir = ROOT / "sftp_avl2db" / "parsers"
        banned = ("import paramiko", "import sqlite3", "import loguru",
                  "import portalocker", "logger.", "socket.")
        for p in sorted(parser_dir.glob("*.py")):
            src = p.read_text(encoding="utf-8")
            for word in banned:
                self.assertNotIn(word, src, f"{p.name} 不应包含 {word}")

    def test_subprocess_loads_no_third_party_modules(self):
        """无 .env、清空环境变量的子进程里跑真实样本解析，不加载第三方模块。"""
        code = r'''
import sys, os
from pathlib import Path
repo, samples = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
os.environ.clear()

from sftp_avl2db.parsers import FlatColumnParser, ContinuationParser

p = Path(samples)
for f in sorted(p.glob("F0955*.AVL")):
    list(FlatColumnParser(12).parse(f))
for f in sorted(p.glob("F0959*.AVL")):
    list(ContinuationParser().parse(f))

banned = ("paramiko", "sqlite3", "loguru", "portalocker")
loaded = [m for m in banned if m in sys.modules]
if loaded:
    raise AssertionError("解析层加载了禁用模块: " + repr(loaded))
print("PARSER_PURE")
'''
        r = subprocess.run(
            [sys.executable, "-c", code, str(ROOT), str(SAMPLES)],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, f"stderr:\n{r.stderr}\nstdout:\n{r.stdout}")
        self.assertIn("PARSER_PURE", r.stdout)

    def test_no_db_and_no_network_calls(self):
        """打桩：解析真实样本期间不调用 sqlite3.connect / socket.create_connection。"""
        with mock.patch("sqlite3.connect",
                        side_effect=AssertionError("解析层不应建库")), \
             mock.patch("socket.create_connection",
                        side_effect=AssertionError("解析层不应连网")):
            for name in ("F0955420261110000000.AVL",
                         "F0959120261110000000.AVL"):
                parser = (FlatColumnParser(12) if name.startswith("F0955")
                          else ContinuationParser())
                self.assertGreater(len(list(parser.parse(SAMPLES / name))), 0)


class FlatParserEdgeCaseTests(unittest.TestCase):

    def test_column_count_padding_and_int_conversion(self):
        rows, st = _flat("a\tb\t123\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 12)
        self.assertEqual(rows[0][2], "123")
        self.assertEqual(list(rows[0][3:11]), [""] * 8)
        self.assertEqual(rows[0][11], 0)          # 补出的空串 → int 兜底 0
        self.assertEqual(st.rows, 1)
        self.assertEqual(st.skipped, 0)

    def test_column_count_truncation(self):
        fields = [f"c{i}" for i in range(12)] + ["EXTRA"]
        rows, _ = _flat("\t".join(fields) + "\n")
        self.assertEqual(len(rows[0]), 12)
        self.assertNotIn("EXTRA", rows[0])
        self.assertEqual(rows[0][11], 0)          # c11 非数字 → 0

    def test_city_id_non_numeric_and_empty(self):
        fields = ["op", "call", "123"] + ["x"] * 8 + ["not_a_number"]
        rows, _ = _flat("\t".join(fields) + "\n")
        self.assertEqual(rows[0][11], 0)
        fields[-1] = ""
        rows, _ = _flat("\t".join(fields) + "\n")
        self.assertEqual(rows[0][11], 0)
        fields[-1] = " 7 "
        rows, _ = _flat("\t".join(fields) + "\n")
        self.assertEqual(rows[0][11], 7)

    def test_empty_product_no_is_skipped(self):
        fields = ["op", "call", "   "] + ["x"] * 8 + ["3"]
        rows, st = _flat("\t".join(fields) + "\n")
        self.assertEqual(rows, [])
        self.assertEqual(st.rows, 0)
        self.assertEqual(st.skipped, 1)

    def test_blank_lines_and_strip(self):
        fields = ["op", "call", "123"] + ["x"] * 8 + ["3"]
        rows, st = _flat("\n  \n   " + "\t".join(fields) + "   \n\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "op")        # 行首空格被 strip
        self.assertEqual(st.rows, 1)

    def test_encoding_probe_is_preserved_as_is(self):
        """编码探测逐字照搬：errors='ignore' 下实际恒走 utf-8，不做修正。"""
        self.assertEqual(ENCODING_CANDIDATES, ("utf-8", "gbk", "latin-1"))
        rows, st = _flat("op\tcall\t123\t中文\n", encoding="gbk")
        self.assertEqual(st.encoding, "utf-8")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "123")
        # gbk 字节在 utf-8 + errors=ignore 下会被吞掉，这正是现状语义
        self.assertNotIn("中文", rows[0][3])

    def test_stats_reset_between_files(self):
        parser = FlatColumnParser(12)
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2 = Path(tmp) / "a.AVL", Path(tmp) / "b.AVL"
            p1.write_text("op\tcall\t123\n", encoding="utf-8")
            p2.write_text("op\tcall\t456\n", encoding="utf-8")
            self.assertEqual(len(list(parser.parse(p1))), 1)
            self.assertEqual(parser.stats.rows, 1)
            self.assertEqual(len(list(parser.parse(p2))), 1)
            self.assertEqual(parser.stats.rows, 1)     # 不累加，已 reset

    def test_invalid_column_count(self):
        with self.assertRaises(ValueError):
            FlatColumnParser(0)


class ContinuationParserEdgeCaseTests(unittest.TestCase):

    def test_strip_eol_removes_exactly_one_backslash(self):
        """核心保真：连续两个反斜杠只剥一个，不能用 rstrip。"""
        self.assertEqual(strip_eol("abc\n"), "abc")
        self.assertEqual(strip_eol("abc\\\n"), "abc")
        self.assertEqual(strip_eol("abc\\\\\n"), "abc\\")
        self.assertEqual(strip_eol("abc\r\n"), "abc\r")   # 不剥 \r

    def test_split_head_shapes(self):
        self.assertEqual(split_head("d\tu\tp\tc\ts"),
                         ["d", "u", "p", "c", "s"])
        self.assertEqual(split_head("d\tu\tp\tc\ts\textra"),
                         ["d", "u", "p", "c", "s"])      # >=5 列丢弃第 6 列起
        self.assertEqual(split_head("d\tu\tp\tc"),
                         ["d", "u", "p", "c", ""])       # ==4 列补空
        self.assertIsNone(split_head("d\tu\tp"))          # <4 列不可用

    def test_continuation_lines_joined_with_newline(self):
        rows, st = _cont(
            "2026-11-09\tu1\t13800138000\t11\tfirst\\\n"
            "second\\\n"
            "third\\\n"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], "first\nsecond\nthird")
        self.assertEqual(st.rows, 1)
        self.assertEqual(st.skipped, 0)

    def test_double_backslash_keeps_one(self):
        rows, _ = _cont("2026-11-09\tu1\t13800138000\t11\tfirst\\\\\n")
        self.assertEqual(rows[0][4], "first\\")

    def test_four_column_head_gets_empty_strategy(self):
        rows, _ = _cont("2026-11-09\tu1\t13800138000\t11\n")
        self.assertEqual(rows[0][4], "")
        self.assertEqual(len(rows[0]), 5)

    def test_malformed_head_discarded(self):
        rows, st = _cont("2026-11-09\tu1\t13800138000\n")
        self.assertEqual(rows, [])
        self.assertEqual(st.malformed_heads, 1)

    def test_orphan_continuation_at_file_start_discarded(self):
        rows, st = _cont(
            "orphan line\\\n"
            "2026-11-09\tu1\t13800138000\t11\tok\n"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], "ok")
        self.assertEqual(st.orphan_continuations, 1)

    def test_blank_physical_line_does_not_add_newline(self):
        """真实样本最后一行是单个反斜杠；剥完为空应跳过，不多拼 \\n。"""
        rows, _ = _cont(
            "2026-11-09\tu1\t13800138000\t11\tok\n"
            "\\\n"
        )
        self.assertEqual(rows[0][4], "ok")

    def test_empty_product_no_is_skipped(self):
        rows, st = _cont("2026-11-09\tu1\t\t11\tok\n")
        self.assertEqual(rows, [])
        self.assertEqual(st.skipped, 1)

    def test_multiple_records_and_final_flush(self):
        rows, st = _cont(
            "2026-11-09\tu1\tA\t11\tone\\\n"
            "tail1\n"
            "2026-11-10\tu2\tB\t22\ttwo\\\n"
            "tail2\n"
        )
        self.assertEqual(st.rows, 2)
        self.assertEqual([r[2] for r in rows], ["A", "B"])
        self.assertEqual(rows[0][4], "one\ntail1")
        self.assertEqual(rows[1][4], "two\ntail2")

    def test_five_plus_columns_drop_extra(self):
        rows, _ = _cont("2026-11-09\tu1\t13800138000\t11\tcol5\tcol6\tcol7\n")
        self.assertEqual(len(rows[0]), 5)
        self.assertEqual(rows[0][4], "col5")

    def test_encoding_is_fixed_utf8(self):
        _, st = _cont("2026-11-09\tu1\t138\t11\tok\n", encoding="gbk")
        self.assertEqual(st.encoding, "utf-8")

    def test_stats_reset_between_files(self):
        parser = ContinuationParser()
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2 = Path(tmp) / "a.AVL", Path(tmp) / "b.AVL"
            p1.write_text("2026-11-09\tu\tA\t11\tx\n", encoding="utf-8")
            p2.write_text("2026-11-09\tu\tB\t11\ty\n", encoding="utf-8")
            self.assertEqual(len(list(parser.parse(p1))), 1)
            self.assertEqual(parser.stats.rows, 1)
            self.assertEqual(len(list(parser.parse(p2))), 1)
            self.assertEqual(parser.stats.rows, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
