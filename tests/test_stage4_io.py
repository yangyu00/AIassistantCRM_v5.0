# -*- coding: utf-8 -*-
"""阶段 4 校验：Remote / State / Logger（标准库 unittest，零新增依赖）。

覆盖：
1. ``FetchedManifest``：追加 + fsync、重复 add 不落盘、重启重建、空行忽略、
   非 ASCII 文件名、``StateStore`` 协议；
2. ``SingleInstanceLock``：非阻塞锁、锁文件 JSON 字段、同进程锁冲突、
   release 幂等、portalocker / fcntl 两个分支、双后端缺失时明确失败；
3. ``LocalDirRemoteSource``：完整远程路径取 basename、下载、缺失抛错、no-op 连接；
4. ``SftpRemoteSource``：用注入的假 paramiko 客户端验证连接参数、惰性连接、
   ``exists`` 三态、``get`` 调用、``close`` 顺序与异常吞掉、可重连；
5. ``TaskLogger``：日志目录与两个文件名、sink 参数逐项、
   **初始化期间不调用 ``remove()``**、「只移除自己的 sink」、
   info/error/warning 转发与 ``extra.task``、filter 行为、缺 loguru 时明确报错；
6. 零副作用：三个模块 import 不加载 paramiko / loguru / portalocker，
   模块级无第三方 import，日志层不改全局 logger。

运行（仓库根目录）：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sftp_avl2db.state as state_mod                       # noqa: E402
from sftp_avl2db.logging import (                           # noqa: E402
    LOG_FORMAT, InMemoryLogger, TaskLogger,
)
from sftp_avl2db.protocols import RemoteSource, StateStore   # noqa: E402
from sftp_avl2db.remote import (                             # noqa: E402
    LocalDirRemoteSource, SftpRemoteSource,
)
from sftp_avl2db.state import (                              # noqa: E402
    FetchedManifest, SingleInstanceLock,
)


class FetchedManifestTests(unittest.TestCase):

    def test_empty_start_and_directory_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "nested" / "state" / "t.fetched"
            m = FetchedManifest(p)
            self.assertTrue(p.parent.is_dir())
            self.assertFalse(m.has("X.AVL"))
            self.assertEqual(len(m), 0)

    def test_add_has_and_no_duplicate_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.fetched"
            m = FetchedManifest(p)
            m.add("F0955420261110000000.AVL")
            m.add("F0955420261110000000.AVL")       # 重复
            m.add("F0959120261110000000.AVL")
            self.assertTrue(m.has("F0955420261110000000.AVL"))
            self.assertEqual(len(m), 2)
            lines = p.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, ["F0955420261110000000.AVL",
                                     "F0959120261110000000.AVL"])
            self.assertEqual(list(m), sorted(lines))
            self.assertIn("F0959120261110000000.AVL", m)

    def test_restart_rebuilds_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.fetched"
            FetchedManifest(p).add("A.AVL")
            m2 = FetchedManifest(p)
            self.assertTrue(m2.has("A.AVL"))
            m2.add("B.AVL")
            m3 = FetchedManifest(p)
            self.assertEqual(list(m3), ["A.AVL", "B.AVL"])

    def test_blank_lines_ignored_and_unicode_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.fetched"
            p.write_text("\n  \nA.AVL\n\t\n", encoding="utf-8")
            m = FetchedManifest(p)
            self.assertEqual(list(m), ["A.AVL"])
            m.add("前缀_20261110.AVL")
            self.assertIn("前缀_20261110.AVL",
                          p.read_text(encoding="utf-8"))

    def test_fsync_is_called(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.fetched"
            m = FetchedManifest(p)
            with mock.patch.object(state_mod.os, "fsync",
                                   wraps=state_mod.os.fsync) as fs:
                m.add("A.AVL")
                self.assertEqual(fs.call_count, 1)
            with mock.patch.object(state_mod.os, "fsync") as fs2:
                m.add("A.AVL")                    # 重复 add 不应写盘
                fs2.assert_not_called()

    def test_satisfies_state_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(FetchedManifest(Path(tmp) / "t.fetched"),
                                  StateStore)


class SingleInstanceLockTests(unittest.TestCase):

    def test_acquire_writes_pid_host_start_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "nested" / "t.lock"
            lock = SingleInstanceLock(p)
            self.assertTrue(lock.acquire())
            self.assertTrue(p.parent.is_dir())
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"pid", "host", "start_time"})
            self.assertEqual(data["pid"], os.getpid())
            self.assertIsInstance(data["host"], str)
            self.assertRegex(data["start_time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
            lock.release()

    def test_release_is_idempotent_and_reacquirable(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = SingleInstanceLock(Path(tmp) / "t.lock")
            self.assertTrue(lock.acquire())
            lock.release()
            lock.release()                        # 幂等
            self.assertTrue(lock.acquire())       # 释放后可再次拿到
            lock.release()

    def test_lock_conflict_same_process(self):
        """同进程两个 fd 对同一文件 flock：第二个应非阻塞失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.lock"
            a, b = SingleInstanceLock(p), SingleInstanceLock(p)
            self.assertTrue(a.acquire())
            self.assertFalse(b.acquire())         # 拿不到立即 False
            a.release()
            self.assertTrue(b.acquire())
            b.release()

    def test_fcntl_branch_forced(self):
        """强制 portalocker=None，验证走 fcntl 分支。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(state_mod, "_portalocker", None), \
                 mock.patch.object(state_mod._fcntl, "flock",
                                   wraps=state_mod._fcntl.flock) as fl:
                lock = SingleInstanceLock(Path(tmp) / "t.lock")
                self.assertTrue(lock.acquire())
                self.assertGreaterEqual(fl.call_count, 1)
                lock.release()

    def test_portalocker_branch_forced(self):
        """注入假 portalocker，验证 lock / unlock 调用。"""
        fake = SimpleNamespace(
            LOCK_EX=2, LOCK_NB=4, LOCK_UN=8,
            lock=mock.Mock(), unlock=mock.Mock())
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(state_mod, "_portalocker", fake):
                lock = SingleInstanceLock(Path(tmp) / "t.lock")
                self.assertTrue(lock.acquire())
                fake.lock.assert_called_once()
                lock.release()
                fake.unlock.assert_called_once()

    def test_no_backend_returns_false_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(state_mod, "_portalocker", None), \
                 mock.patch.object(state_mod, "_fcntl", None):
                lock = SingleInstanceLock(Path(tmp) / "t.lock")
                self.assertFalse(lock.acquire())


class LocalDirRemoteSourceTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "remote"
        self.src.mkdir()
        (self.src / "F0955420261110000000.AVL").write_bytes(b"a\tb\tc\n")
        self.remote = LocalDirRemoteSource(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exists_with_full_remote_path(self):
        full = "/ftpdata2/data_reverse/wangsi/aifile/F0955420261110000000.AVL"
        self.assertTrue(self.remote.exists(full))
        self.assertFalse(self.remote.exists(
            "/ftpdata2/data_reverse/wangsi/aifile/F0999920261110000000.AVL"))
        self.assertTrue(self.remote.exists("F0955420261110000000.AVL"))

    def test_fetch_copies_bytes_and_creates_parent(self):
        dst = Path(self.tmp.name) / "files" / "task" / "F0955420261110000000.AVL"
        self.remote.connect()
        self.remote.fetch("/x/y/F0955420261110000000.AVL", dst)
        self.assertEqual(dst.read_bytes(), b"a\tb\tc\n")
        self.assertEqual((self.src / "F0955420261110000000.AVL").read_bytes(),
                         b"a\tb\tc\n")              # 源文件未被改动
        self.remote.close()

    def test_fetch_missing_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            self.remote.fetch("/x/y/MISSING.AVL",
                              Path(self.tmp.name) / "out.AVL")

    def test_connect_close_noop_and_protocol(self):
        self.assertIsNone(self.remote.connect())
        self.assertIsNone(self.remote.close())
        self.assertIsInstance(self.remote, RemoteSource)


class _FakeSftp:
    def __init__(self, stat_error=None):
        self.stat_error = stat_error
        self.stat_calls = []
        self.get_calls = []
        self.closed = False

    def stat(self, path):
        self.stat_calls.append(path)
        if self.stat_error is not None:
            raise self.stat_error
        return SimpleNamespace(st_size=100)

    def get(self, remote, local):
        self.get_calls.append((remote, local))
        Path(local).write_bytes(b"downloaded\n")

    def close(self):
        self.closed = True


class _FakeSsh:
    def __init__(self, sftp, open_error=None):
        self._sftp = sftp
        self.open_error = open_error
        self.policy = None
        self.connect_args = None
        self.closed = False

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, *args, **kwargs):
        self.connect_args = args
        if kwargs:
            raise AssertionError(f"旧代码 _connect 不用关键字参数: {kwargs}")

    def open_sftp(self):
        if self.open_error:
            raise self.open_error
        return self._sftp

    def close(self):
        self.closed = True


class SftpRemoteSourceTests(unittest.TestCase):

    def _conf(self):
        return {"host": "134.80.199.252", "port": 22,
                "username": "wangsi", "password": "secret",
                "remote_path": "/ftpdata2/data_reverse/wangsi/aifile"}

    def test_connect_arguments_match_legacy(self):
        sftp, ssh = _FakeSftp(), None
        ssh = _FakeSsh(sftp)
        policy = object()
        src = SftpRemoteSource(self._conf(), ssh_client=ssh,
                               auto_add_policy=policy)
        src.connect()
        self.assertEqual(ssh.connect_args,
                         ("134.80.199.252", 22, "wangsi", "secret"))
        self.assertIs(ssh.policy, policy)

    def test_lazy_connect_only_once(self):
        sftp, ssh = _FakeSftp(), None
        ssh = _FakeSsh(sftp)
        src = SftpRemoteSource(self._conf(), ssh_client=ssh,
                               auto_add_policy=object())
        self.assertTrue(src.exists("/r/A.AVL"))
        self.assertTrue(src.exists("/r/B.AVL"))
        self.assertEqual(len(sftp.stat_calls), 2)
        self.assertEqual(ssh.connect_args,
                         ("134.80.199.252", 22, "wangsi", "secret"))
        src.close()

    def test_exists_three_states(self):
        # 成功 → True
        sftp, ssh = _FakeSftp(), None
        ssh = _FakeSsh(sftp)
        src = SftpRemoteSource(self._conf(), ssh_client=ssh,
                               auto_add_policy=object())
        self.assertTrue(src.exists("/r/A.AVL"))
        src.close()

        # FileNotFoundError → False
        sftp = _FakeSftp(stat_error=FileNotFoundError("not generated"))
        src = SftpRemoteSource(self._conf(), ssh_client=_FakeSsh(sftp),
                               auto_add_policy=object())
        self.assertFalse(src.exists("/r/A.AVL"))
        src.close()

        # 其他异常 → 透传
        sftp = _FakeSftp(stat_error=OSError("network down"))
        src = SftpRemoteSource(self._conf(), ssh_client=_FakeSsh(sftp),
                               auto_add_policy=object())
        with self.assertRaises(OSError):
            src.exists("/r/A.AVL")

    def test_fetch_and_close_order_and_reconnect(self):
        sftp, ssh = _FakeSftp(), None
        ssh = _FakeSsh(sftp)
        src = SftpRemoteSource(self._conf(), ssh_client=ssh,
                               auto_add_policy=object())
        dst = Path(tempfile.mkdtemp()) / "sub" / "A.AVL"
        src.fetch("/r/A.AVL", dst)
        self.assertEqual(sftp.get_calls, [("/r/A.AVL", str(dst))])
        self.assertEqual(dst.read_bytes(), b"downloaded\n")
        src.close()
        self.assertTrue(sftp.closed)
        self.assertTrue(ssh.closed)
        # close 后可再次 connect（下轮重连）
        src.connect()
        self.assertTrue(src.exists("/r/B.AVL"))
        src.close()

    def test_open_sftp_failure_closes_ssh(self):
        ssh = _FakeSsh(_FakeSftp(), open_error=RuntimeError("open failed"))
        src = SftpRemoteSource(self._conf(), ssh_client=ssh,
                               auto_add_policy=object())
        with self.assertRaises(RuntimeError):
            src.connect()
        self.assertTrue(ssh.closed)

    def test_close_swallows_exceptions(self):
        class Bad:
            def close(self):
                raise RuntimeError("close boom")
        src = SftpRemoteSource(self._conf(), ssh_client=_FakeSsh(_FakeSftp()),
                               auto_add_policy=object())
        src.connect()
        src._sftp = Bad()
        src._ssh = Bad()
        src.close()                  # 不应抛
        self.assertIsNone(src._sftp)

    def test_protocol(self):
        src = SftpRemoteSource(self._conf(), ssh_client=_FakeSsh(_FakeSftp()),
                               auto_add_policy=object())
        self.assertIsInstance(src, RemoteSource)


class TaskLoggerTests(unittest.TestCase):

    def _paths(self, sinks):
        return sorted(Path(v["sink"]).name for v in sinks.values())

    def test_two_sink_paths_and_directory_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "tasks" / "log"
            fake = InMemoryLogger()
            tl = TaskLogger("opportunity_recall", log_dir,
                            logger=fake, day="2026-11-18")
            self.assertTrue(log_dir.is_dir())
            self.assertEqual(self._paths(fake.sinks), [
                "opportunity_recall_ERROR_2026-11-18.log",
                "opportunity_recall_INFO_2026-11-18.log",
            ])
            self.assertEqual(tl.info_log_path.name,
                             "opportunity_recall_INFO_2026-11-18.log")
            tl.close()

    def test_sink_kwargs_match_legacy_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = InMemoryLogger()
            tl = TaskLogger("loss_control", Path(tmp),
                            logger=fake, day="2026-11-18")
            by_level = {v["kwargs"]["level"]: v["kwargs"]
                        for v in fake.sinks.values()}
            self.assertEqual(set(by_level), {"INFO", "ERROR"})
            for kw in by_level.values():
                self.assertEqual(kw["rotation"], "00:00")
                self.assertEqual(kw["retention"], "7 day")
                self.assertEqual(kw["encoding"], "utf8")
                self.assertEqual(kw["format"], LOG_FORMAT)
                self.assertIn("filter", kw)
            self.assertNotIn("backtrace", by_level["INFO"])
            self.assertTrue(by_level["ERROR"]["backtrace"])
            self.assertTrue(by_level["ERROR"]["diagnose"])
            tl.close()

    def test_no_remove_during_init_and_close_only_own_sinks(self):
        """核心：不清空宿主日志；close 只移除自己注册的两个 sink。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake = InMemoryLogger()
            host_sink_id = fake.add("host://existing", level="DEBUG")
            before = dict(fake.sinks)

            tl = TaskLogger("opportunity_recall", Path(tmp),
                            logger=fake, day="2026-11-18")
            self.assertEqual(fake.remove_calls, [],
                             "初始化期间不应调用 logger.remove()")
            self.assertIn(host_sink_id, fake.sinks, "宿主 sink 被清空了")
            self.assertEqual(len(fake.sinks), 3)

            tl.close()
            self.assertEqual(fake.remove_calls,
                             [sid for sid in fake.remove_calls if sid != host_sink_id])
            self.assertIn(host_sink_id, fake.sinks, "close 误删了宿主 sink")
            self.assertEqual(len(fake.sinks), 1)
            self.assertEqual(before[host_sink_id], fake.sinks[host_sink_id])

    def test_close_does_not_remove_other_tasks_sinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = InMemoryLogger()
            a = TaskLogger("opportunity_recall", Path(tmp),
                           logger=fake, day="2026-11-18")
            b = TaskLogger("loss_control", Path(tmp),
                           logger=fake, day="2026-11-18")
            self.assertEqual(len(fake.sinks), 4)
            a.close()
            self.assertEqual(len(fake.sinks), 2)
            self.assertEqual(self._paths(fake.sinks), [
                "loss_control_ERROR_2026-11-18.log",
                "loss_control_INFO_2026-11-18.log",
            ])
            b.close()
            self.assertEqual(fake.sinks, {})

    def test_emit_forwards_with_task_extra(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = InMemoryLogger()
            tl = TaskLogger("loss_control", Path(tmp),
                            logger=fake, day="2026-11-18")
            tl.info("连接成功")
            tl.error("下载失败")
            tl.warning("注意")
            self.assertEqual([(r["level"], r["message"])
                              for r in fake.records],
                             [("INFO", "连接成功"), ("ERROR", "下载失败"),
                              ("WARNING", "注意")])
            self.assertTrue(all(r["extra"] == {"task": "loss_control"}
                                for r in fake.records))
            tl.close()

    def test_filter_isolates_level_and_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = InMemoryLogger()
            tl = TaskLogger("loss_control", Path(tmp),
                            logger=fake, day="2026-11-18")
            info_f = next(v["kwargs"]["filter"] for v in fake.sinks.values()
                          if v["kwargs"]["level"] == "INFO")
            err_f = next(v["kwargs"]["filter"] for v in fake.sinks.values()
                         if v["kwargs"]["level"] == "ERROR")

            def rec(level, task):
                return {"level": SimpleNamespace(name=level),
                        "extra": {"task": task}}

            self.assertTrue(info_f(rec("INFO", "loss_control")))
            self.assertFalse(info_f(rec("ERROR", "loss_control")))
            self.assertFalse(info_f(rec("INFO", "opportunity_recall")))
            self.assertTrue(err_f(rec("ERROR", "loss_control")))
            self.assertFalse(err_f(rec("ERROR", "opportunity_recall")))
            tl.close()

    def test_missing_loguru_gives_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(sys.modules, {"loguru": None}):
                with self.assertRaisesRegex(RuntimeError, "loguru"):
                    TaskLogger("x", Path(tmp))
            # 注入替身则完全不需要 loguru
            with mock.patch.dict(sys.modules, {"loguru": None}):
                tl = TaskLogger("x", Path(tmp), logger=InMemoryLogger())
                tl.info("ok")
                tl.close()

    def test_day_defaults_to_today(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            fake = InMemoryLogger()
            tl = TaskLogger("opportunity_recall", Path(tmp), logger=fake)
            self.assertIn(time.strftime("%Y-%m-%d"), tl.info_log_path.name)
            tl.close()


class ZeroSideEffectTests(unittest.TestCase):

    def test_no_top_level_third_party_imports(self):
        pattern = re.compile(r"^(?:import|from)\s+(paramiko|loguru|portalocker)\b",
                             re.M)
        for name in ("remote.py", "state.py", "logging.py"):
            src = (ROOT / "sftp_avl2db" / name).read_text(encoding="utf-8")
            self.assertIsNone(pattern.search(src),
                              f"{name} 模块级不应 import 第三方库")

    def test_import_does_not_load_third_party_modules(self):
        code = r'''
import sys, os, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
os.environ.clear()

import sftp_avl2db.remote, sftp_avl2db.state, sftp_avl2db.logging
from sftp_avl2db.logging import InMemoryLogger, TaskLogger
from sftp_avl2db.remote import LocalDirRemoteSource
from sftp_avl2db.state import FetchedManifest, SingleInstanceLock

banned = ("paramiko", "loguru", "portalocker")
loaded = [m for m in banned if m in sys.modules]
if loaded:
    raise AssertionError("阶段 4 模块 import 加载了第三方库: " + repr(loaded))

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    src = root / "remote"; src.mkdir()
    (src / "A.AVL").write_bytes(b"x\n")
    r = LocalDirRemoteSource(src)
    assert r.exists("/x/y/A.AVL")
    r.fetch("/x/y/A.AVL", root / "files" / "A.AVL")
    m = FetchedManifest(root / "state" / "t.fetched")
    m.add("A.AVL")
    lock = SingleInstanceLock(root / "state" / "t.lock")
    assert lock.acquire(); lock.release()
    lg = InMemoryLogger()
    tl = TaskLogger("demo", root / "log", logger=lg, day="2026-11-18")
    tl.info("ok"); tl.close()

loaded = [m for m in banned if m in sys.modules]
if loaded:
    raise AssertionError("离线组件运行期间加载了第三方库: " + repr(loaded))
print("STAGE4_PURE")
'''
        r = subprocess.run([sys.executable, "-c", code, str(ROOT)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0,
                         f"stderr:\n{r.stderr}\nstdout:\n{r.stdout}")
        self.assertIn("STAGE4_PURE", r.stdout)

    def test_offline_source_does_not_touch_network(self):
        """离线源跑完整流程期间不应调用 socket.create_connection。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "remote"
            src.mkdir()
            (src / "A.AVL").write_bytes(b"x\n")
            remote = LocalDirRemoteSource(src)
            with mock.patch("socket.create_connection",
                            side_effect=AssertionError("不应连网")):
                self.assertTrue(remote.exists("/x/y/A.AVL"))
                remote.fetch("/x/y/A.AVL", root / "files" / "A.AVL")
                remote.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
