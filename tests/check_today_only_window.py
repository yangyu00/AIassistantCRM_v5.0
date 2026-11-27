"""「当天文件已到达就只处理当天」日期窗口自检（零第三方依赖，不连 SFTP）。

覆盖：当天文件在时跳过前一天 / 当天文件不在时仍兜底「昨天 + 今天」/
``TODAY_FILE_WINDOW_SCOPE=off`` 可一键回退旧行为 /
``date_strings`` 注入不受探测影响 / 探测失败按「未到达」保守回退 /
控损断供兜底的「应到文件」跟着切到当天，不误判断供。

用法：``python3 tests/check_today_only_window.py``
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sftp_avl2db.runner as runner_mod                     # noqa: E402
from sftp_avl2db.config import load_profiles                 # noqa: E402
from sftp_avl2db.profiles import build_task                  # noqa: E402
from sftp_avl2db.task_logging import InMemoryLogger         # noqa: E402

NOW = datetime(2026, 11, 26, 2, 0, 0)
AVL = "F09627"                       # 固定表、3 列，跑一轮最轻
YESTERDAY, TODAY, DAY_BEFORE = "20261125", "20261126", "20261124"
NAME_Y = f"{AVL}{YESTERDAY}000000.AVL"
NAME_T = f"{AVL}{TODAY}000000.AVL"
NAME_D = f"{AVL}{DAY_BEFORE}000000.AVL"
LOSS_T = f"F09591{TODAY}000000.AVL"

TMP = Path(tempfile.mkdtemp(prefix="today_window_"))
_failures = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}'
          + (f" → {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


@contextmanager
def scope(value):
    old = runner_mod.TODAY_FILE_WINDOW_SCOPE
    runner_mod.TODAY_FILE_WINDOW_SCOPE = value
    try:
        yield
    finally:
        runner_mod.TODAY_FILE_WINDOW_SCOPE = old


class FakeRemote:
    """内存远端替身：记录每轮真正 download 了哪些文件。"""

    def __init__(self, files, *, probe_fail=()):
        self.files = dict(files)
        self.fetched = []
        self.exists_calls = []
        self.probe_fail = set(probe_fail)

    def connect(self):
        return None

    def exists(self, remote_path):
        name = Path(remote_path).name
        self.exists_calls.append(name)
        # 只让「探测那一次」失败，正式 stat 仍成功，模拟瞬时抖动。
        if name in self.probe_fail and self.exists_calls.count(name) == 1:
            raise OSError("模拟探测期 stat 失败")
        return name in self.files

    def fetch(self, remote_path, local_path):
        name = Path(remote_path).name
        self.fetched.append(name)
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(self.files[name], encoding="utf-8")

    def close(self):
        return None


def make_case(tag, files, *, avl=AVL, probe_fail=()):
    runtime = TMP / tag / "runtime"
    profile = load_profiles(runtime_dir=str(runtime))[avl]
    logger = InMemoryLogger()
    task = build_task(profile, logger=logger, sleeper=lambda _: None, now=NOW)
    remote = FakeRemote(files, probe_fail=probe_fail)
    task.runner.source = remote
    return task, profile, logger, remote


def avl_text(op_time, tag):
    return (f"{op_time}\t场景{tag}A\t1\n"
            f"{op_time}\t场景{tag}B\t2\n")


def both_files():
    return {NAME_Y: avl_text("2026-11-25", "Y"),
            NAME_T: avl_text("2026-11-26", "T")}


def manifest(profile):
    p = Path(profile.fetched_path)
    return p.read_text(encoding="utf-8").split() if p.is_file() else []


def logs_contains(logger, keyword):
    return any(keyword in r["message"] for r in logger.records)


def main():
    print("=== 1. 当天文件已到达：只处理当天，无视前一天 ===")
    with scope("each_round"):
        task, profile, logger, remote = make_case("both", both_files())
        task.runner.serve_forever(max_rounds=1)
        check("只 fetch 当天文件", remote.fetched == [NAME_T],
              str(remote.fetched))
        check("前一天文件不登记清单", NAME_Y not in manifest(profile))
        check("当天文件已登记清单", NAME_T in manifest(profile))
        check("日志说明本轮只处理当天",
              logs_contains(logger, "本轮只处理 20261126，跳过前一天"))
        conn = sqlite3.connect(str(profile.db_dir / f"{AVL}.db"))
        try:
            rows = conn.execute(f"SELECT op_time FROM {AVL}").fetchall()
        finally:
            conn.close()
        check("库内只剩当天批次（没有昨天→今天的二次重灌）",
              bool(rows) and all(r[0] == "2026-11-26" for r in rows),
              str(rows))

    print("=== 2. 当天文件未到达：仍兜底「昨天 + 今天」 ===")
    with scope("each_round"):
        task2, profile2, _, remote2 = make_case(
            "only_yesterday", {NAME_Y: avl_text("2026-11-25", "Y")})
        task2.runner.serve_forever(max_rounds=1)
        check("前一天文件正常入库", NAME_Y in manifest(profile2),
              str(remote2.fetched))
        check("当天文件只是 MISSING，不产生下载",
              remote2.fetched == [NAME_Y], str(remote2.fetched))

    print("=== 3. 每轮生效：连续两轮都不回头灌前一天 ===")
    with scope("each_round"):
        task3, _, _, remote3 = make_case("two_rounds", both_files())
        task3.runner.serve_forever(max_rounds=2)
        check("两轮都只拉当天（第二轮命中清单 SKIP）",
              remote3.fetched == [NAME_T], str(remote3.fetched))
        check("前一天文件全程没被下载", NAME_Y not in remote3.fetched,
              str(remote3.fetched))

    print("=== 4. off：回到旧行为，每轮固定「昨天 + 今天」 ===")
    with scope("off"):
        task4, _, _, remote4 = make_case("off", both_files())
        task4.runner.serve_forever(max_rounds=1)
        check("两天文件按日期升序各拉一次",
              remote4.fetched == [NAME_Y, NAME_T], str(remote4.fetched))

    print("=== 5. date_strings 注入：完全不受当天探测影响 ===")
    with scope("each_round"):
        task5, _, _, remote5 = make_case(
            "inject", dict(both_files(), **{NAME_D: avl_text("2026-11-24", "D")}))
        task5.runner.serve_forever(max_rounds=1, date_strings=[DAY_BEFORE])
        check("只拉注入的那一天", remote5.fetched == [NAME_D],
              str(remote5.fetched))

    print("=== 6. 探测期 stat 失败：按「未到达」保守回退到双日期 ===")
    with scope("each_round"):
        task6, _, logger6, remote6 = make_case(
            "probe_fail", both_files(), probe_fail=(NAME_T,))
        task6.runner.serve_forever(max_rounds=1)
        check("仍然兜底拉了前一天",
              remote6.fetched == [NAME_Y, NAME_T], str(remote6.fetched))
        check("探测失败有 ERROR 日志",
              logs_contains(logger6, "探测当天 AVL 文件失败"))

    print("=== 7. 控损断供兜底：本轮「应到文件」跟着切到当天 ===")
    with scope("each_round"):
        task7, _, logger7, _ = make_case(
            "loss", {LOSS_T: ""}, avl="F09591")
        task7.runner.serve_forever(max_rounds=1)
        check("_expected_remote_name() 返回当天文件",
              task7.runner._expected_remote_name() == LOSS_T,
              str(task7.runner._expected_remote_name()))
        check("断供兜底日志里的应到文件是当天",
              logs_contains(logger7, f"应到文件 {LOSS_T}"))

    return not _failures


if __name__ == "__main__":
    try:
        passed = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("=== ALL PASS ===" if passed else
          f"=== FAILED: {len(_failures)} 项 ===")
    sys.exit(0 if passed else 1)
