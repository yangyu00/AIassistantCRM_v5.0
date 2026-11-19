# -*- coding: utf-8 -*-
"""阶段 5 校验：编排 + 接线（标准库 unittest，零新增依赖）。

覆盖：
1. 端到端：``LocalDirRemoteSource(io_samples)`` → ``Avl2DbRunner`` →
   与 ``golden.json`` **4 库 / 131 表**逐字段一致，``log_signals`` 对齐；
2. 断点清单：首次 4 个 ok → 二次 SKIP imported=0 → ``--force-reload`` 再 4 个；
3. 两个接线决策：未知前缀按失败不登记；``exists()`` 三态按旧日志映射；
4. 连接 / stat / 下载失败语义；
5. 锁生效（同进程第二个实例拿不到）与 release 后可再跑；
6. ``serve_forever`` 单次 / 常驻（注入时钟 + sleeper）、每轮清理；
7. ``cleanup_expired`` 本地 AVL + DB 过期清理；
8. CLI 日期校验 / 未知任务 / 正常退出码；``python -m sftp_avl2db --help``；
9. 在线 ``services.data_query.query()`` 返回值与 golden 行一致，``main.py`` 零改动；
10. ``import sftp_avl2db`` 零 I/O、延迟导出可用。

运行（仓库根目录）：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crm_dbspec                                            # noqa: E402
import services.data_query as dq                             # noqa: E402
from sftp_avl2db import (                                    # noqa: E402
    Avl2DbRunner, InMemoryLogger, LocalDirRemoteSource,
    TaskProfileConfig, build_task,
)
from sftp_avl2db.cli import main as cli_main                 # noqa: E402
from sftp_avl2db.logging import TaskLogger                  # noqa: E402
from sftp_avl2db.remote import SftpRemoteSource              # noqa: E402
from sftp_avl2db.sink import UnknownPrefixError              # noqa: E402

GOLDEN = Path(os.environ.get("SFTP_AVL2DB_GOLDEN")
              or ROOT.parent.parent / "baseline" / "golden.json")
SAMPLES = ROOT / "io_samples"
DATE = "20261110"
FAKE_SFTP = {"host": "h", "port": 22, "username": "u",
             "password": "p", "remote_path": "/ftpdata2/data_reverse/wangsi/aifile"}
# 注入时钟：让控损 DATA_EXPIRE_DAYS=3 的 cutoff=2026-11-07，
# 样本 op_time=2026-11-09 不会被清理，从而 golden 比对不被 cleanup 干扰。
FIXED_NOW = datetime(2026, 11, 10, 12, 0, 0)


def _profile(runtime, name, *, now=None):
    return TaskProfileConfig.from_mapping({
        "name": name,
        "file_prefixes": (["F09554", "F09555", "F09556"]
                          if name == "opportunity_recall" else ["F09591"]),
        "sftp": FAKE_SFTP,
        "runtime_dir": str(runtime),
        "schema": crm_dbspec.DB_SCHEMAS[name],
        "parser": "flat" if name == "opportunity_recall" else "continuation",
        "routing": "per_prefix" if name == "opportunity_recall" else "crc32_shards",
        "num_tables": 128 if name == "loss_control" else 0,
        "table_name_prefix": "data_F09591" if name == "loss_control" else "",
    })


def _build(runtime, name, *, logger=None, local_dir=SAMPLES,
           now=FIXED_NOW, sleeper=None):
    profile = _profile(runtime, name)
    log = logger or InMemoryLogger()
    task = build_task(profile, logger=log, local_dir=local_dir,
                      now=now, sleeper=sleeper)
    return task, log


def _norm_rows(rows):
    out = [list(r) for r in rows]
    out.sort(key=lambda r: tuple("" if v is None else str(v) for v in r))
    return out


def _dump_db(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result = {}
        for t in tables:
            info = [[x[1], x[2], x[5]]
                    for x in conn.execute(f'PRAGMA table_info("{t}")')]
            rows = _norm_rows(conn.execute(f'SELECT * FROM "{t}"').fetchall())
            result[t] = {
                "columns": info,
                "row_count": len(rows),
                "rows_sha256": hashlib.sha256(
                    json.dumps(rows, ensure_ascii=False, sort_keys=True,
                               default=str).encode("utf-8")).hexdigest(),
                "rows": rows,
            }
        return {"tables": result}
    finally:
        conn.close()


def _dump_all(root: Path) -> dict:
    return {db.name: _dump_db(db) for db in sorted(root.rglob("*.db"))}


def _messages(log) -> list[str]:
    return [r["message"] for r in log.records]


def _row(n: int, op_time="2026-11-15") -> tuple:
    """造一条商机 12 列记录（本文件自带，避免跨测试模块依赖）。"""
    return (op_time, "2026-11-06", f"1390000{n:04d}", "大类", "小类",
            "摘要", "需求", "策略", "无", "备注", "参考", 3)


@unittest.skipUnless(SAMPLES.is_dir() and GOLDEN.is_file(),
                     f"缺少样本或 golden：{SAMPLES} / {GOLDEN}")
class EndToEndTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def _run_both(self, runtime):
        counts = {}
        for name in ("opportunity_recall", "loss_control"):
            task, log = _build(runtime, name)
            counts[name] = (task.runner.run_once_for_date(DATE), log)
            task.close()
        return counts

    def test_run_once_matches_golden_131_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            counts = self._run_both(runtime)
            self.assertEqual(counts["opportunity_recall"][0], 3)
            self.assertEqual(counts["loss_control"][0], 1)

            got = _dump_all(runtime / "sqlite_db")
            want = {Path(k).name: v for k, v in self.golden["databases"].items()}
            self.assertEqual(set(got), set(want))
            total = 0
            for db_name, wdb in want.items():
                gdb = got[db_name]
                self.assertEqual(set(gdb["tables"]), set(wdb["tables"]))
                for table, wtv in wdb["tables"].items():
                    gtv = gdb["tables"][table]
                    total += 1
                    self.assertEqual(gtv["columns"], wtv["columns"],
                                     f"{db_name}.{table} 列定义")
                    self.assertEqual(gtv["row_count"], wtv["row_count"],
                                     f"{db_name}.{table} 行数")
                    self.assertEqual(gtv["rows_sha256"], wtv["rows_sha256"],
                                     f"{db_name}.{table} 内容")
                    self.assertEqual(gtv["rows"], wtv["rows"])
            self.assertEqual(total, 131)

    def test_log_signals_match_golden(self):
        """两类计数文本与 golden.log_signals 逐项对齐。"""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            counts = self._run_both(runtime)
            for task_name, sig in self.golden["log_signals"].items():
                text = "\n".join(_messages(counts[task_name][1]))
                totals = [int(x) for x in re.findall(r"共\s*(\d+)\s*条", text)]
                skips = [int(x) for x in
                         re.findall(r"跳过空手机号\s*(\d+)\s*条", text)]
                self.assertEqual(totals[:len(sig["total_seen"])],
                                 sig["total_seen"], task_name)
                self.assertEqual(skips[:len(sig["skipped_seen"])],
                                 sig["skipped_seen"], task_name)

    def test_manifest_skip_and_force_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            task, log = _build(runtime, "opportunity_recall")

            self.assertEqual(task.runner.run_once_for_date(DATE), 3)
            names = sorted(Path(task.profile.fetched_path).read_text(
                encoding="utf-8").splitlines())
            self.assertEqual(len(names), 3)
            self.assertTrue(all(n.endswith(".AVL") for n in names))

            # 二次：清单命中 → SKIP，imported=0
            log.records.clear()
            self.assertEqual(task.runner.run_once_for_date(DATE), 0)
            self.assertEqual(sum("SKIP 已在清单中" in m
                                 for m in _messages(log)), 3)

            # force_reload：重新下载入库，清单不重复追加
            log.records.clear()
            self.assertEqual(task.runner.run_once_for_date(DATE,
                                                          force_reload=True), 3)
            self.assertEqual(len(Path(task.profile.fetched_path).read_text(
                encoding="utf-8").splitlines()), 3)
            self.assertEqual(len([m for m in _messages(log)
                                  if "入库完成并登记清单" in m]), 3)
            task.close()

    def test_manifest_restart_keeps_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            t1, _ = _build(runtime, "loss_control")
            self.assertEqual(t1.runner.run_once_for_date(DATE), 1)
            t1.close()
            t2, log2 = _build(runtime, "loss_control")   # 新实例读回清单
            self.assertEqual(t2.runner.run_once_for_date(DATE), 0)
            self.assertIn("SKIP 已在清单中", "\n".join(_messages(log2)))
            t2.close()


class FailureSemanticsTests(unittest.TestCase):

    def test_unknown_prefix_is_failure_not_registered(self):
        """接线决策 1：UnknownPrefixError → failed、不登记清单、可重试。"""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            task, log = _build(runtime, "opportunity_recall")
            src = runtime / "remote"
            src.mkdir()
            # 文件名不匹配任何已注册前缀，但内容合法
            (src / "F9999920261110000000.AVL").write_text(
                "2026-11-08\t2026-11-06\t139\tA\tB\tx\tx\tx\tx\tx\tx\t3\n",
                encoding="utf-8")
            task.runner.source = LocalDirRemoteSource(src)
            task.sink.ensure_schema()      # import_one 由上层建表，这里显式补上
            # 直接调 import_one，绕过「上层只下发已注册前缀」的保护
            result = task.runner.import_one(DATE, "F99999")
            self.assertEqual(result.status, "failed")
            self.assertFalse(task.state.has("F9999920261110000000.AVL"))
            self.assertTrue(any("解析入库失败" in m for m in _messages(log)))
            task.close()

    def test_exists_three_states_log_mapping(self):
        """接线决策 2：False→INFO 尚未生成；其他异常→ERROR stat 出错。"""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            empty = runtime / "empty_remote"      # 不存在 → exists() 全为 False
            task, log = _build(runtime, "opportunity_recall", local_dir=empty)

            r_missing = task.runner.import_one(DATE, "F09554")
            self.assertEqual(r_missing.status, "missing")
            self.assertTrue(any("远程文件尚未生成" in m for m in _messages(log)))
            self.assertFalse(task.state.has(r_missing.filename))

            log.records.clear()
            with mock.patch.object(task.source, "exists",
                                   side_effect=OSError("network down")):
                r_err = task.runner.import_one(DATE, "F09555")
            self.assertEqual(r_err.status, "failed")
            self.assertTrue(any("stat 远程文件出错" in m for m in _messages(log)))
            self.assertFalse(task.state.has(r_err.filename))
            task.close()

    def test_download_failure_not_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            task, log = _build(runtime, "opportunity_recall")
            with mock.patch.object(task.source, "fetch",
                                   side_effect=OSError("get failed")):
                r = task.runner.import_one(DATE, "F09554")
            self.assertEqual(r.status, "failed")
            self.assertTrue(any("下载失败" in m for m in _messages(log)))
            self.assertFalse(task.state.has(r.filename))
            task.close()

    def test_connect_failure_returns_zero(self):
        class BrokenSource(LocalDirRemoteSource):
            def connect(self):
                raise RuntimeError("连接失败")

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            task, log = _build(runtime, "opportunity_recall")
            task.runner.source = BrokenSource(SAMPLES)
            self.assertEqual(task.runner.run_once_for_date(DATE), 0)
            self.assertTrue(any("SFTP 连接失败" in m for m in _messages(log)))
            task.close()

    def test_import_one_result_fields(self):
        from sftp_avl2db import ImportResult
        with tempfile.TemporaryDirectory() as tmp:
            task, _ = _build(tmp, "loss_control")
            task.sink.ensure_schema()
            r = task.runner.import_one(DATE, "F09591")
            self.assertIsInstance(r, ImportResult)
            self.assertEqual(r.status, "ok")
            self.assertEqual(r.rows, 2)
            self.assertEqual(r.skipped, 0)
            task.close()


class LockAndLoopTests(unittest.TestCase):

    def test_lock_conflict_returns_1_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            t1, log1 = _build(runtime, "opportunity_recall")
            t2, log2 = _build(runtime, "opportunity_recall")
            self.assertTrue(t1.lock.acquire())
            rc = t2.runner.serve_forever(only_date=DATE)
            self.assertEqual(rc, 1)
            self.assertTrue(any("获取任务锁失败" in m for m in _messages(log2)))
            t1.lock.release()
            # 释放后 t2 能正常跑
            rc2 = t2.runner.serve_forever(only_date=DATE)
            self.assertEqual(rc2, 0)
            t1.close(); t2.close()

    def test_serve_forever_single_date_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            task, log = _build(runtime, "loss_control")
            rc = task.runner.serve_forever(only_date=DATE)
            self.assertEqual(rc, 0)
            self.assertTrue(any("守护进程启动" in m for m in _messages(log)))
            self.assertTrue(any("单次模式结束" in m for m in _messages(log)))
            # 固定时钟下样本 op_time=2026-11-09 不应被清理
            dump = _dump_all(runtime / "sqlite_db")
            self.assertEqual(dump["F09591.db"]["tables"]
                             ["data_F09591_36"]["row_count"], 1)
            # 锁已释放
            self.assertTrue(task.lock.acquire())
            task.lock.release()
            task.close()

    def test_serve_forever_yesterday_and_today_with_max_rounds(self):
        sleeps = []
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            task, log = _build(runtime, "opportunity_recall",
                               sleeper=lambda s: sleeps.append(s))
            rc = task.runner.serve_forever(max_rounds=2,
                                           date_strings=["20261109", "20261110"])
            self.assertEqual(rc, 0)
            self.assertEqual(sleeps, [600, 600])
            # 每轮两个日期，两个日期都试过；只有 20261110 有文件
            self.assertEqual(len([m for m in _messages(log)
                                  if "远程文件尚未生成" in m]), 6)
            self.assertEqual(len([m for m in _messages(log)
                                  if "本轮结束" in m]), 2)
            task.close()

    def test_keyboard_interrupt_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, log = _build(tmp, "opportunity_recall")
            with mock.patch.object(task.runner, "run_once_for_date",
                                   side_effect=KeyboardInterrupt):
                rc = task.runner.serve_forever(only_date=DATE)
            self.assertEqual(rc, 0)
            self.assertTrue(any("收到中断信号" in m for m in _messages(log)))
            task.close()


class CleanupTests(unittest.TestCase):

    def test_cleanup_removes_old_local_avl_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            task, log = _build(runtime, "opportunity_recall")
            files = task.profile.local_dir
            files.mkdir(parents=True, exist_ok=True)
            old = files / "OLD.AVL"
            old.write_text("x\n", encoding="utf-8")
            os.utime(old, (1_000_000_000, 1_000_000_000))  # 2001 年
            fresh = files / "FRESH.AVL"
            fresh.write_text("x\n", encoding="utf-8")
            other = files / "OLD.txt"
            other.write_text("x\n", encoding="utf-8")
            os.utime(other, (1_000_000_000, 1_000_000_000))

            out = task.runner.cleanup_expired()
            self.assertIn("OLD.AVL", out["removed_local_files"])
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(other.exists(), "非 .AVL 文件不应删除")
            self.assertEqual(out["errors"], [])
            task.close()

    def test_cleanup_purges_expired_db_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            task, _ = _build(runtime, "opportunity_recall")
            task.sink.ensure_schema()
            task.sink.write("F0955420261110000000.AVL",
                            [_row(1, "2026-01-01"), _row(2, "2026-11-09")],
                            router=task.layout)
            # 注入 now=2026-11-10，DATA_EXPIRE_DAYS=30 → cutoff=2026-10-11
            out = task.runner.cleanup_expired()
            self.assertEqual(out["cutoff"], "2026-10-11")
            self.assertEqual(out["purged_rows"], 1)
            left = _dump_all(runtime / "sqlite_db")["F09554.db"]["tables"]["data_F09554"]
            self.assertEqual(left["row_count"], 1)
            self.assertEqual(left["rows"][0][0], "2026-11-09")
            task.close()


class CliTests(unittest.TestCase):

    def test_bad_date_returns_2(self):
        self.assertEqual(cli_main(["--task", "loss_control",
                                  "--date", "2026-11-10"]), 2)

    def test_unknown_task_returns_2(self):
        rc = cli_main(["--task", "no_such_task"],
                      profiles={"loss_control": object()})
        self.assertEqual(rc, 2)

    def test_cli_single_date_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            profile = _profile(runtime, "loss_control")
            log = InMemoryLogger()
            rc = cli_main(
                ["--task", "loss_control", "--date", DATE,
                 "--local-dir", str(SAMPLES)],
                profiles={"loss_control": profile},
                logger=log, sleeper=lambda s: None, now=FIXED_NOW)
            self.assertEqual(rc, 0)
            self.assertEqual(_dump_all(runtime / "sqlite_db")["F09591.db"]
                             ["tables"]["data_F09591_36"]["row_count"], 1)
            self.assertTrue(any("单次模式结束" in m for m in _messages(log)))

    def test_module_help_without_env(self):
        """--help 不应读 .env；子进程在空目录里跑。"""
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, "-m", "sftp_avl2db", "--help"],
                cwd=tmp, capture_output=True, text=True,
                env={"PYTHONPATH": str(ROOT), "PATH": os.environ.get("PATH", "")},
            )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--force-reload", r.stdout)
        self.assertIn("--task", r.stdout)


class OnlineQueryTests(unittest.TestCase):
    """接线后在线读取侧结果不变，且 main.py 零改动。"""

    def test_query_opportunity_recall_after_new_writer(self):
        import asyncio
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            task, _ = _build(runtime, "opportunity_recall")
            self.assertEqual(task.runner.run_once_for_date(DATE), 3)
            task.close()
            with mock.patch.object(dq, "PROFILES",
                                   {"opportunity_recall": task.profile}):
                out = asyncio.run(dq.get_opportunity_recall_latest(
                    "13954262012", "202608041615335173"))
            self.assertTrue(out)
            self.assertEqual(out["product_no"], "13954262012")
            self.assertEqual(out["city_id"], 3)
            self.assertEqual(list(out), crm_dbspec.db_columns("opportunity_recall"))

    def test_query_loss_control_after_new_writer(self):
        import asyncio
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            task, _ = _build(runtime, "loss_control")
            self.assertEqual(task.runner.run_once_for_date(DATE), 1)
            task.close()
            with mock.patch.object(dq, "PROFILES",
                                   {"loss_control": task.profile}):
                out = asyncio.run(dq.get_loss_control_latest("17854339544"))
            self.assertTrue(out)
            self.assertEqual(out["product_no"], "17854339544")
            self.assertTrue(str(out["strategy_name"]).startswith("<$B>一、客户诊断"))
            self.assertEqual(list(out), crm_dbspec.db_columns("loss_control"))

    def test_product_ids_unchanged(self):
        self.assertEqual(
            set(dq.OPPORTUNITY_RECALL_PRODUCT_IDS),
            set(crm_dbspec.QUERY_SPECS["opportunity_recall"]["db_map"]))
        self.assertEqual(
            set(dq.LOSS_CONTROL_PRODUCT_IDS),
            set(crm_dbspec.QUERY_SPECS["loss_control"]["product_ids"]))

    def test_main_py_has_no_tasks_import(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("import tasks", src)
        self.assertNotIn("from tasks", src)
        self.assertIn("from services import data_query", src)

    def test_old_tasks_package_is_gone(self):
        self.assertFalse((ROOT / "tasks" / "config.py").exists())
        self.assertFalse((ROOT / "tasks" / "base_task.py").exists())
        self.assertTrue((ROOT / "tasks").is_dir(),
                        "tasks/ 应保留为运行时数据目录")


class PackageIntegrityTests(unittest.TestCase):

    def test_lazy_exports_work(self):
        import sftp_avl2db as pkg
        for name in ("build_task", "Avl2DbRunner", "TaskLogger",
                     "FetchedManifest", "SqliteSink", "ContinuationParser"):
            self.assertTrue(hasattr(pkg, name), name)
            self.assertIn(name, pkg.__all__)

    def test_import_zero_side_effects(self):
        code = r'''
import builtins, os, sys
from pathlib import Path
repo = sys.argv[1]
sys.path.insert(0, repo)
opened = []
real_open = builtins.open
def t(file, *a, **kw):
    opened.append(str(file)); return real_open(file, *a, **kw)
builtins.open = t
real_read = Path.read_text
def tr(self, *a, **kw):
    opened.append(str(self)); return real_read(self, *a, **kw)
Path.read_text = tr
import sftp_avl2db
if any(".env" in p for p in opened):
    raise AssertionError("import 期间读了 .env")
banned = [m for m in ("paramiko", "loguru") if m in sys.modules]
if banned:
    raise AssertionError("import 加载了: " + repr(banned))
print("PKG_CLEAN")
'''
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, "-c", code, str(ROOT)],
                               cwd=tmp, capture_output=True, text=True,
                               env={"PYTHONPATH": str(ROOT),
                                    "PATH": os.environ.get("PATH", "")})
        self.assertEqual(r.returncode, 0,
                         f"stderr:\n{r.stderr}\nstdout:\n{r.stdout}")
        self.assertIn("PKG_CLEAN", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
