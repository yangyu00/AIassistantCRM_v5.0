"""F09633 启动库损坏自检与重建自检（零第三方依赖，全部落在临时目录）。

不打任何服务、不连 SFTP / 在线服务。覆盖：

1. 健康库 / 库不存在：不重建、不误删，正常进入建表流程；
2. 文件头损坏（corrupt）与原子切换半套 schema（incomplete）：都能判定并
   直接删除损坏库（含 -wal / -shm / -journal 残留、不留任何副本）
   + 重建 128 张空表 + _avl_meta；
3. 重建后 `.fetched` 清单与失败隔离标记被清空，本轮强制重灌（集成跑一次
   `serve_forever` 单次模式，断言真的入库、水位推进，而不是 SKIP）；
4. 全量页校验开关：默认不跑 `PRAGMA quick_check`（8 亿行会吃掉窗口），
   置 1 才跑且健康结果标注；
5. 改回安全模式后靠 `*.unsafe_mode` 标记补做一次检查，通过后标记自动删除；
6. 普通 AVL / 无标记不触发；sink 替身缺检查方法时只 WARNING 不炸；
7. `FetchedManifest.clear()` 截断并 fsync、内存集合同步清空。

用法：``python3 tests/check_f09633_startup_rebuild.py``
"""
from __future__ import annotations

import shutil
import sqlite3 as sqlite3_mod
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sftp_avl2db.config import load_profiles                 # noqa: E402
from sftp_avl2db.profiles import build_task                  # noqa: E402
from sftp_avl2db.state import FetchedManifest               # noqa: E402
from sftp_avl2db.task_logging import InMemoryLogger         # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="f09633_rebuild_"))
DATE = "20261120"
NAME = f"F09633{DATE}000000.AVL"
ROWS = [
    ("2026-11-20", "u1", "15054675009", "超套", "痛点A"),
    ("2026-11-20", "u2", "13800000001", "流量不够用", "痛点B"),
]
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


class FakeRemote:
    """只按文件名给内容的远端替身。"""

    def __init__(self, files):
        self.files = dict(files)

    def connect(self):
        return None

    def exists(self, remote_path):
        return Path(remote_path).name in self.files

    def fetch(self, remote_path, local_path):
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(self.files[Path(remote_path).name], encoding="utf-8")

    def close(self):
        return None


def avl_text(rows):
    return "".join("\t".join(r) + "\n" for r in rows)


def make_task(tag, *, unsafe=True, full=0, files=None):
    profile = load_profiles(runtime_dir=str(TMP / tag))["F09633"]
    if full:
        profile = replace(profile, startup_full_check=full)
    if not unsafe:
        profile = replace(profile, unsafe_no_durability=0)
    memo = InMemoryLogger()
    task = build_task(profile, logger=memo, sleeper=lambda _: None,
                      unsafe_no_durability=unsafe)
    task.runner.source = FakeRemote(files or {})
    return task, memo, profile


def logs(memo, level=None, needle=None):
    return [r["message"] for r in memo.records
            if (level is None or r["level"] == level)
            and (needle is None or needle in r["message"])]


def table_names(db):
    conn = sqlite3_mod.connect(str(db))
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def row_count(db, table):
    conn = sqlite3_mod.connect(str(db))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def seed(task, rows=ROWS):
    """建库并灌一批数据，返回 db 路径。"""
    task.sink.ensure_schema()
    task.sink.write(NAME, rows)
    return task.sink.layout.db_path


def expected_tables(task):
    return sorted(t for _, t in task.sink.layout.all_targets())


def main():
    print("=== 1. 健康库 / 库不存在：不误删不重建 ===")
    t1, m1, p1 = make_task("healthy")
    db1 = seed(t1)
    before_tables = table_names(db1)
    before_rows = sum(row_count(db1, t) for t in expected_tables(t1))
    t1.runner._startup_db_health_check()
    check("健康库检查后数据行数不变",
          sum(row_count(db1, t) for t in expected_tables(t1)) == before_rows
          == len(ROWS), f"{before_rows}")
    check("健康库检查后表集合不变", table_names(db1) == before_tables)
    check("健康库不产生 .corrupt.* 副本",
          not list(db1.parent.glob("F09633.db.corrupt.*")))
    check("健康检查日志为 healthy",
          any("healthy" in x for x in logs(m1, "INFO", "启动库健康检查")),
          str(logs(m1, "INFO", "启动库健康检查")))
    t1.close()

    t2, m2, p2 = make_task("missing")
    db2 = t2.sink.layout.db_path
    t2.runner._startup_db_health_check()
    check("库不存在判定 missing，检查本身不建库", not db2.is_file())
    check("库不存在不产生副本 / 不误报损坏",
          not list(db2.parent.glob("F09633.db.corrupt.*"))
          and not logs(m2, "ERROR"))
    t2.sink.ensure_schema()
    check("missing 之后正常建表 128 张 + _avl_meta",
          len(table_names(db2) & set(expected_tables(t2))) == 128
          and "_avl_meta" in table_names(db2))
    t2.close()

    print("=== 2. 文件头损坏 → 直接删除 + 重建空库 + 清清单 / 隔离标记 ===")
    t3, m3, p3 = make_task("corrupt")
    db3 = seed(t3)
    t3.state.add(NAME)                       # 模拟旧清单会 SKIP
    t3.runner._quarantine().add(NAME)        # 模拟之前被失败隔离
    t3.runner._write_quarantine()
    db3.write_bytes(b"GARBAGE-NOT-A-SQLITE-DB" * 8)
    t3.runner._startup_db_health_check()
    with db3.open("rb") as f:
        head = f.read(16)
    check("损坏库重建后是合法 SQLite 且 128 张表 + meta 齐全",
          db3.is_file() and head == b"SQLite format 3\x00"
          and len(table_names(db3) & set(expected_tables(t3))) == 128
          and "_avl_meta" in table_names(db3))
    check("重建后业务表为空（旧数据已丢）",
          sum(row_count(db3, t) for t in expected_tables(t3)) == 0)
    check("损坏库直接删除、不保留任何 .corrupt.* 副本",
          not list(db3.parent.glob("F09633.db.corrupt.*")),
          str([x.name for x in db3.parent.glob("F09633.db.corrupt.*")]))
    check("ERROR 日志明确说明未保留副本",
          any("未保留任何副本" in x for x in logs(m3, "ERROR")),
          str(logs(m3, "ERROR")[:2]))
    check("重建后 .fetched 清单已清空（否则永远 SKIP）",
          len(t3.state) == 0 and not t3.state.has(NAME))
    check("重建后失败隔离标记已清空",
          not t3.runner._quarantine()
          and not (p3.state_dir / "F09633.drop_quarantine").read_text(
              encoding="utf-8").strip())
    check("重建打了 ERROR 告警且说明数据丢失",
          any("上一日全量数据" in x for x in logs(m3, "ERROR")),
          str(logs(m3, "ERROR")[:2]))
    t3.close()

    print("=== 2b. WAL / SHM / journal 残留一起删除，不留任何副本 ===")
    t3b, m3b, p3b = make_task("siblings")
    db3b = seed(t3b)
    leftovers = [Path(str(db3b) + s)
                 for s in ("-wal", "-shm", "-journal")]
    for p in leftovers:
        p.write_bytes(b"STALE-JOURNAL-RESIDUE" * 4)
    db3b.write_bytes(b"BROKEN" * 32)
    t3b.runner._startup_db_health_check()
    check("重建后仍是合法空库（128 张表 + _avl_meta）",
          db3b.is_file()
          and "_avl_meta" in table_names(db3b)
          and len(table_names(db3b) & set(expected_tables(t3b))) == 128)
    check("-wal / -shm / -journal 残留全部被删除",
          all(not p.exists() for p in leftovers),
          str([p.name for p in leftovers if p.exists()]))
    check("目录里没有任何 .corrupt.* 副本",
          not list(db3b.parent.glob("F09633.db.corrupt.*")))
    check("处置日志说明删除文件数与未保留副本",
          any("未保留任何副本" in x and "已直接删除" in x
              for x in logs(m3b, "ERROR")), str(logs(m3b, "ERROR")[:2]))
    t3b.close()

    print("=== 3. 半切换 schema（缺表）→ incomplete → 重建 ===")
    t4, m4, p4 = make_task("incomplete")
    db4 = seed(t4)
    conn = sqlite3_mod.connect(str(db4))
    try:
        for t in expected_tables(t4)[:3]:
            conn.execute(f"DROP TABLE {t}")
        conn.commit()
    finally:
        conn.close()
    t4.runner._startup_db_health_check()
    check("缺表被判 incomplete 并重建 128 张表",
          len(table_names(db4) & set(expected_tables(t4))) == 128)
    check("incomplete 日志含「原子切换事务中途崩溃」提示",
          any("半" in x or "切换" in x for x in logs(m4, "ERROR")),
          str(logs(m4, "ERROR")[:2]))
    t4.close()

    print("=== 4. 集成：重建后本轮强制重灌，不是 SKIP ===")
    t5, m5, p5 = make_task("rerun", files={NAME: avl_text(ROWS)})
    db5 = seed(t5)
    t5.state.add(NAME)
    t5.runner._quarantine().add(NAME)
    t5.runner._write_quarantine()
    db5.write_bytes(b"BROKEN" * 32)
    rc = t5.runner.serve_forever(only_date=DATE, force_reload=False)
    check("serve_forever 单次模式返回 0", rc == 0, str(rc))
    check("重建后重新拉取并入库（清单重新登记）",
          t5.state.has(NAME), str(list(t5.state)))
    check("新批次数据已在当前套",
          sum(row_count(db5, t) for t in expected_tables(t5)) == len(ROWS),
          str(sum(row_count(db5, t) for t in expected_tables(t5))))
    conn = sqlite3_mod.connect(str(db5))
    try:
        meta = conn.execute(
            "SELECT latest_file_date FROM _avl_meta WHERE avl='F09633'"
        ).fetchone()
    finally:
        conn.close()
    check("_avl_meta 水位推进到本批", meta == (DATE,), str(meta))
    check("本轮没有 SKIP「已在清单中」",
          not any("已在清单中" in x for x in logs(m5, "INFO")),
          str(logs(m5, "INFO", "SKIP")))
    t5.close()

    print("=== 5. 全量页校验开关：默认不跑 quick_check ===")
    t6, _, p6 = make_task("quick_off")
    db6 = seed(t6)
    calls = []
    orig = sqlite3_mod.connect

    def spy(*args, **kwargs):
        c = orig(*args, **kwargs)
        c.set_trace_callback(calls.append)
        return c

    sqlite3_mod.connect = spy
    try:
        h_off = t6.sink.startup_health_check(full=False)
    finally:
        sqlite3_mod.connect = orig
    check("full=0 健康且标注未跑全量校验",
          h_off.status == "healthy" and "未跑全量页校验" in h_off.reason,
          h_off.reason)
    check("full=0 未执行 PRAGMA quick_check",
          not any("quick_check" in s for s in calls), str(calls[-3:]))
    h_on = t6.sink.startup_health_check(full=True)
    check("full=1 执行 quick_check 且通过",
          h_on.status == "healthy" and "quick_check 通过" in h_on.reason,
          h_on.reason)
    t6.close()

    t7, m7, p7 = make_task("quick_env", full=1)
    db7 = seed(t7)
    t7.runner._startup_db_health_check()
    check("profile.startup_full_check=1 经 runner 透传到检查",
          p7.startup_full_check == 1
          and any("quick_check 通过" in x
                  for x in logs(m7, "INFO", "启动库健康检查")),
          str(logs(m7, "INFO", "启动库健康检查")))
    t7.close()

    print("=== 6. 改回安全模式后靠标记补做一次，通过后删除 ===")
    t8, m8, p8 = make_task("restored", unsafe=False)
    marker = p8.state_dir / "F09633.unsafe_mode"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("2026-11-27 09:00:00", encoding="utf-8")
    t8.sink.ensure_schema()
    t8.runner._startup_db_health_check()
    check("安全模式 + 标记存在 → 仍做一次检查",
          any("healthy" in x for x in logs(m8, "INFO", "启动库健康检查")))
    check("检查通过后标记自动删除", not marker.is_file())
    count_before = len(logs(m8, "INFO", "启动库健康检查"))
    t8.runner._startup_db_health_check()
    check("标记删除后再次启动不再检查",
          len(logs(m8, "INFO", "启动库健康检查")) == count_before)
    t8.close()

    print("=== 7. 不触发条件与替身兜底 ===")
    prof9 = load_profiles(runtime_dir=str(TMP / "other"))["F09591"]
    memo9 = InMemoryLogger()
    t9 = build_task(prof9, logger=memo9, sleeper=lambda _: None)
    t9.runner._startup_db_health_check()
    check("普通 AVL（无标记 / 非 unsafe）完全不检查",
          not logs(memo9, needle="启动库健康检查"),
          str(logs(memo9)))
    t9.close()

    t10, m10, _ = make_task("stub", unsafe=True)
    t10.runner.sink = SimpleNamespace(unsafe_no_durability=True)
    t10.runner._startup_db_health_check()
    check("sink 替身无检查方法 → WARNING 跳过、不抛异常",
          any("跳过" in x for x in logs(m10, "WARNING"))
          and not logs(m10, "ERROR"), str(logs(m10)))
    t10.close()

    print("=== 8. FetchedManifest.clear() 契约 ===")
    mf = TMP / "manifest_clear" / "x.fetched"
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text("A.AVL\nB.AVL\n", encoding="utf-8")
    st = FetchedManifest(mf)
    check("clear 前读到 2 条", len(st) == 2 and st.has("A.AVL"))
    st.clear()
    check("clear 后内存集合与文件都为空",
          len(st) == 0 and not st.has("A.AVL")
          and mf.read_text(encoding="utf-8") == "")
    st.add("C.AVL")
    st2 = FetchedManifest(mf)
    check("clear 后仍可正常 add 且重新加载只读到新条目",
          st.has("C.AVL") and list(st2) == ["C.AVL"], str(list(st2)))

    print("=== 结论 ===")
    if _failures:
        print("FAILED: %d 项 → %s" % (len(_failures), _failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(rc)
