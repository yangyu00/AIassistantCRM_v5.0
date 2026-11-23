# -*- coding: utf-8 -*-
"""AVL 批次新鲜度窗口自检（零第三方依赖，不连 SFTP / IOP / 在线服务）。

覆盖：写入侧 _avl_meta 水位登记、补跑旧文件不回退、窗口下界计算、
单条查询倒序取最新、多条查询收敛到最新批次、关闭 / 缺表降级。
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services import db_config, reader                     # noqa: E402
from sftp_avl2db.config import load_profiles               # noqa: E402
from sftp_avl2db.parsers import build_parser               # noqa: E402
from sftp_avl2db.sink import AVL_META_TABLE, build_sink    # noqa: E402

AVL = "F09627"
SAMPLE = ROOT / "io_samples" / "F0962720261120000000.AVL"
TMP = Path(tempfile.mkdtemp(prefix="avl_freshness_"))
_failures = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f" → {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def meta_row(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            f"SELECT avl, latest_file_date, latest_op_time, last_filename "
            f"FROM {AVL_META_TABLE}").fetchall()
    finally:
        conn.close()


def configure(window):
    cfg = db_config.load(
        avl_db_root=TMP / "sqlite_db",
        dimensions_db=TMP / "dimensions.db",
        freshness_window_days=window)
    reader.configure(cfg)
    db_config.validate(cfg)
    return cfg


def main():
    print("=== 1. 写入侧登记批次水位 ===")
    profile = load_profiles(runtime_dir=str(TMP))[AVL]
    sink = build_sink(profile)
    sink.ensure_schema()
    rows = list(build_parser(profile).parse(SAMPLE))
    n = sink.write(SAMPLE.name, rows)
    db = profile.db_dir / f"{AVL}.db"

    check("样例 10 行入库", n == 10, f"{n} 行")
    check("write() 统计出批次最大 op_time",
          sink.stats.max_op_time == "2026-11-19", sink.stats.max_op_time)
    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    check("元数据表随 ensure_schema() 建出", AVL_META_TABLE in tables,
          str(sorted(tables)))
    m = meta_row(db)
    check("_avl_meta 只有一行", len(m) == 1, str(m))
    check("水位 = 文件名日期 20261120 / 数据日期 2026-11-19",
          len(m) == 1 and m[0][1] == "20261120"
          and m[0][2] == "2026-11-19", str(m))

    print("=== 2. 补跑旧文件不把水位拉回去 ===")
    older = [("2026-11-09", "补跑旧场景", 99)]
    sink.write("F0962720261110000000.AVL", older)
    m2 = meta_row(db)
    check("旧日期文件不覆盖最新水位",
          len(m2) == 1 and m2[0][1] == "20261120"
          and m2[0][2] == "2026-11-19", str(m2))
    conn = sqlite3.connect(str(db))
    try:
        old_hit = conn.execute(
            "SELECT 1 FROM F09627 WHERE scene = '补跑旧场景'").fetchone()
    finally:
        conn.close()
    check("旧文件的行仍按主键正常落库", bool(old_hit))

    print("=== 3. 造混合批次数据（19 / 18 / 17 日）===")
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO F09627 (op_time, scene, rn) VALUES (?, ?, ?)",
            ("2026-11-18", "窗口内次新场景", 50))
        conn.execute(
            "INSERT OR REPLACE INTO F09627 (op_time, scene, rn) VALUES (?, ?, ?)",
            ("2026-11-17", "过时场景", 60))
        conn.execute(
            "INSERT OR REPLACE INTO F09627 (op_time, scene, rn) VALUES (?, ?, ?)",
            ("2026-11-19", "最新批次另一场景", 70))
        conn.commit()
    finally:
        conn.close()

    print("=== 4. 窗口 N=2（最新 19 日 → 下界 18 日）===")
    configure(2)
    check("_cutoff() 下界为 18 日",
          reader._cutoff("2026-11-19") == "2026-11-18",
          str(reader._cutoff("2026-11-19")))
    configure(1)
    check("N=1 下界等于最新日期",
          reader._cutoff("2026-11-19") == "2026-11-19",
          str(reader._cutoff("2026-11-19")))
    configure(2)

    many = asyncio.run(reader.query_many(
        AVL, where={"scene": ["点播偏好", "最新批次另一场景",
                              "窗口内次新场景", "过时场景"]}))
    check("多条查询：17 日过时行被窗口排除",
          all(r["op_time"] != "2026-11-17" for r in many))
    check("多条查询：只返回最新 19 日那一批（18 日也被收敛掉）",
          bool(many) and all(r["op_time"] == "2026-11-19" for r in many),
          str(sorted({r["op_time"] for r in many})))
    check("多条查询保留 19 日全部命中行",
          {"点播偏好", "最新批次另一场景"} <= {r["scene"] for r in many},
          str(len(many)))

    single_fresh = asyncio.run(reader.query(AVL, "窗口内次新场景"))
    check("单条查询：窗口内次新批次仍可取到（倒序最新一条）",
          single_fresh.get("op_time") == "2026-11-18", str(single_fresh))
    single_newest = asyncio.run(reader.query(AVL, "点播偏好"))
    check("单条查询：取到 19 日最新一条",
          single_newest.get("op_time") == "2026-11-19", str(single_newest))
    single_stale = asyncio.run(reader.query(AVL, "过时场景"))
    check("单条查询：17 日过时行被窗口排除 → {}",
          single_stale == {}, str(single_stale))

    print("=== 5. 关闭 / 缺表降级 ===")
    configure(0)
    all_rows = asyncio.run(reader.query_many(
        AVL, where={"scene": ["点播偏好", "窗口内次新场景", "过时场景"]}))
    check("N=0 关闭过滤 → 三个批次全返回", len(all_rows) == 3,
          str(sorted(r["op_time"] for r in all_rows)))

    conn = sqlite3.connect(str(db))
    try:
        conn.execute(f"DROP TABLE {AVL_META_TABLE}")
        conn.commit()
    finally:
        conn.close()
    configure(2)
    degraded = asyncio.run(reader.query_many(
        AVL, where={"scene": ["点播偏好", "窗口内次新场景", "过时场景"]}))
    check("元数据表缺失 → 自动降级、不过滤", len(degraded) == 3,
          str(sorted(r["op_time"] for r in degraded)))

    print("=== 6. 配置校验 ===")
    try:
        db_config.load(avl_db_root=TMP / "sqlite_db",
                       freshness_window_days=-1)
        ok = False
    except ValueError:
        ok = True
    check("负窗口被 validate 拦住", ok)
    check("TIME_COLUMN 由静态常量提供",
          db_config.TIME_COLUMN == "op_time"
          and db_config.AVL_META_TABLE == "_avl_meta")

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
