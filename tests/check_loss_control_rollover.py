"""控损（F09591）缺文件兜底自检（零第三方依赖，不连 SFTP / IOP / 在线服务）。

覆盖：远端无当日文件 / 0 字节空文件 / 有内容但 0 条有效记录 / 正常文件、
同一自然日只推进一次、跨天继续续命、非控损 AVL 不参与、非法 op_time
不被写坏、_avl_meta 水位不动、过期清理后前台仍能查到数据。
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services import db_config, reader                       # noqa: E402
from sftp_avl2db.config import load_profiles                 # noqa: E402
from sftp_avl2db.profiles import build_task                  # noqa: E402
from sftp_avl2db.sink import AVL_META_TABLE                 # noqa: E402
from sftp_avl2db.task_logging import InMemoryLogger         # noqa: E402

AVL = "F09591"
NOW = datetime(2026, 11, 26, 2, 0, 0)
NEXT_DAY = datetime(2026, 11, 27, 2, 0, 0)
EXPECTED_FILE = "F0959120261125000000.AVL"   # 26 日 02:00 应拿到 25 日文件
TMP = Path(tempfile.mkdtemp(prefix="loss_roll_"))
_failures = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}'
          + (f" → {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


class FakeRemote:
    """按文件名给内容的远端替身，避免依赖 shutil.copy2 的元数据复制。"""

    def __init__(self, files):
        self.files = dict(files)

    def connect(self):
        return None

    def exists(self, remote_path):
        return Path(remote_path).name in self.files

    def fetch(self, remote_path, local_path):
        name = Path(remote_path).name
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(self.files[name], encoding="utf-8")

    def close(self):
        return None


def make_case(tag, files, *, avl=AVL, now=NOW):
    runtime = TMP / tag / "runtime"
    profile = load_profiles(runtime_dir=str(runtime))[avl]
    logger = InMemoryLogger()
    task = build_task(profile, logger=logger, sleeper=lambda _: None, now=now)
    task.runner.source = FakeRemote(files)
    return task, profile, logger, profile.db_dir / f"{avl}.db"


def avl_text(op_time, n=3, offset=500):
    return "".join(
        f"{op_time}\tu{i}\t1780000{i + offset:04d}\t11\t话术{i}\n"
        for i in range(n))


def seed(task, op_time, n=6, offset=0, filename="F0959120261124000000.AVL"):
    task.sink.ensure_schema()
    rows = [(op_time, f"u{i}", f"1780000{i + offset:04d}", "11", f"话术{i}")
            for i in range(n)]
    return task.sink.write(filename, rows)


def op_times(task, db):
    conn = sqlite3.connect(str(db))
    try:
        out = []
        for _, table in task.layout.all_targets():
            out.extend(r[0] for r in conn.execute(
                f"SELECT op_time FROM {table}"))
        return out
    finally:
        conn.close()


def meta(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            f"SELECT latest_file_date, latest_op_time FROM {AVL_META_TABLE}"
        ).fetchall()
    finally:
        conn.close()


def manifest(profile):
    p = Path(profile.fetched_path)
    return p.read_text(encoding="utf-8").split() if p.is_file() else []


def logs_contains(logger, keyword):
    return any(keyword in r["message"] for r in logger.records)


def main():
    print("=== 1. 远端没有 25 日文件：库内 op_time 整体 +1 天 ===")
    task, profile, logger, db = make_case("missing", {})
    seed(task, "2026-11-23")
    before = meta(db)
    task.runner.serve_forever(max_rounds=1)
    vals = op_times(task, db)
    check("6 行全部由 2026-11-23 推到 2026-11-24",
          vals == ["2026-11-24"] * 6, str(sorted(set(vals))))
    check("sink 统计到 6 行更新", task.sink.stats.rolled_rows == 6,
          str(task.sink.stats.rolled_rows))
    check("_avl_meta 水位不动（在线侧仍按原水位算窗口）",
          meta(db) == before, str(meta(db)))
    check("缺文件不登记清单，下轮可重试",
          EXPECTED_FILE not in manifest(profile))
    check("日志打出了兜底动作", logs_contains(logger, "控损缺文件兜底"))

    print("=== 2. 同一自然日只推进一次 ===")
    task.runner.serve_forever(max_rounds=1)
    check("同日第二轮不再 +1",
          op_times(task, db) == ["2026-11-24"] * 6)

    print("=== 3. 跨天仍断供：继续续命一天 ===")
    task.runner._now = lambda: NEXT_DAY
    task.runner.serve_forever(max_rounds=1)
    check("27 日再推到 2026-11-25",
          op_times(task, db) == ["2026-11-25"] * 6,
          str(sorted(set(op_times(task, db)))))

    print("=== 4. 拉到的 25 日文件是 0 字节：判为异常文件并兜底 ===")
    task2, profile2, logger2, db2 = make_case("empty", {EXPECTED_FILE: ""})
    seed(task2, "2026-11-23")
    before2 = meta(db2)
    task2.runner.serve_forever(max_rounds=1)
    check("空文件触发 op_time +1 天",
          op_times(task2, db2) == ["2026-11-24"] * 6,
          str(sorted(set(op_times(task2, db2)))))
    check("空文件不登记清单（真文件到达后还能重拉）",
          EXPECTED_FILE not in manifest(profile2))
    check("空文件不推进水位", meta(db2) == before2, str(meta(db2)))
    check("日志标记为空文件", logs_contains(logger2, "空文件(0 字节)"))

    print("=== 5. 文件非空但解析出 0 条记录：同样判为异常批次 ===")
    task3, profile3, logger3, db3 = make_case(
        "zero_rows", {EXPECTED_FILE: "孤儿续行，没有日期起始行\n"})
    seed(task3, "2026-11-23")
    task3.runner.serve_forever(max_rounds=1)
    check("空批次触发 op_time +1 天",
          op_times(task3, db3) == ["2026-11-24"] * 6,
          str(sorted(set(op_times(task3, db3)))))
    check("空批次不登记清单", EXPECTED_FILE not in manifest(profile3))
    check("日志标记为空批次", logs_contains(logger3, "无有效记录(空批次)"))

    print("=== 6. 25 日文件正常到达：不触发兜底 ===")
    task4, profile4, _, db4 = make_case(
        "healthy", {EXPECTED_FILE: avl_text("2026-11-24")})
    seed(task4, "2026-11-23")
    task4.runner.serve_forever(max_rounds=1)
    vals4 = op_times(task4, db4)
    check("原有 6 行 op_time 保持 2026-11-23（没有被 +1）",
          vals4.count("2026-11-23") == 6, str(sorted(set(vals4))))
    check("25 日文件 3 行正常入库",
          vals4.count("2026-11-24") == 3, str(sorted(set(vals4))))
    check("正常入库推进水位到 2026-11-24",
          meta(db4) == [("20261125", "2026-11-24")], str(meta(db4)))
    check("正常文件登记清单", EXPECTED_FILE in manifest(profile4))

    print("=== 7. 非法日期文本不被写坏 ===")
    task5, _, _, db5 = make_case("invalid", {})
    seed(task5, "2026-11-23", n=3)
    table5 = next(t for _, t in task5.layout.all_targets())
    conn = sqlite3.connect(str(db5))
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {table5} "
            "(op_time, user_id, product_no, city_id, strategy_name) "
            "VALUES (?, ?, ?, ?, ?)",
            ("not-a-date", "ux", "17800009999", "11", "x"))
        conn.commit()
    finally:
        conn.close()
    task5.runner.serve_forever(max_rounds=1)
    vals5 = op_times(task5, db5)
    check("非法 op_time 原样保留", "not-a-date" in vals5, str(sorted(set(vals5))))
    check("合法行仍正常 +1 天", vals5.count("2026-11-24") == 3)

    print("=== 8. 非控损 AVL 完全不参与 ===")
    task6, _, _, db6 = make_case("other", {}, avl="F09627")
    task6.sink.ensure_schema()
    task6.sink.write("F0962720261124000000.AVL",
                     [("2026-11-23", "场景A", 1)])
    task6.runner.serve_forever(max_rounds=1)
    conn = sqlite3.connect(str(db6))
    try:
        got = conn.execute("SELECT op_time FROM F09627").fetchall()
    finally:
        conn.close()
    check("F09627 的 op_time 不变", got == [("2026-11-23",)], str(got))

    print("=== 9. 前台查询：兜底续住数据，不兜底则被过期清理清空 ===")
    task7, profile7, _, db7 = make_case("frontend", {})
    seed(task7, "2026-11-23")
    task7.runner.serve_forever(max_rounds=1)
    cfg = db_config.load(
        avl_db_root=profile7.db_dir.parent,
        dimensions_db=TMP / "dimensions.db",
        freshness_window_days=2)
    reader.configure(cfg)
    rec = asyncio.run(reader.query(AVL, "17800000000"))
    check("兜底后前台按号码仍查得到（op_time=2026-11-24）",
          bool(rec) and rec.get("op_time") == "2026-11-24", str(rec))

    task8, profile8, _, db8 = make_case("frontend_noroll", {})
    seed(task8, "2026-11-23")
    task8.runner._now = lambda: NEXT_DAY
    task8.runner.cleanup_expired(force_full=True)   # cutoff=2026-11-24，清掉 23 日
    cfg2 = db_config.load(
        avl_db_root=profile8.db_dir.parent,
        dimensions_db=TMP / "dimensions.db",
        freshness_window_days=2)
    reader.configure(cfg2)
    rec2 = asyncio.run(reader.query(AVL, "17800000000"))
    check("对照：不兜底时旧批次被清掉，前台查不到", rec2 == {}, str(rec2))

    return not _failures


if __name__ == "__main__":
    try:
        passed = main()
    finally:
        reader.configure(db_config.load())
        shutil.rmtree(TMP, ignore_errors=True)
    print("=== ALL PASS ===" if passed else
          f"=== FAILED: {len(_failures)} 项 ===")
    sys.exit(0 if passed else 1)
