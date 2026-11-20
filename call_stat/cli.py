# -*- coding: utf-8 -*-
"""命令行入口 ``python -m call_stat``：脱离在线 API 也能查 / 导出 / 落库 / 自检。

所有产物（库、CSV、自检临时库）都落在包内 ``runtime/`` 下；自检用完即清理。
``main()`` 返回 int 而不调用 ``sys.exit``，便于单测直接断言退出码。
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import shutil
import sqlite3
from pathlib import Path

from . import counter
from .config import RUNTIME_DIR, warn
from .query import date_result, month_day_count, month_result, validate_params


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m call_stat",
        description="调用量统计：查询 / 导出 / 落库 / 自检")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--date", default=None, help="单日查询 YYYY-MM-DD")
    g.add_argument("--month", default=None, help="整月查询 YYYY-MM")
    p.add_argument("--export", action="store_true",
                   help="配合 --month：导出 CSV 到 runtime/export/")
    p.add_argument("--flush", action="store_true",
                   help="把本进程未落库增量写入库（幂等）")
    p.add_argument("--self-check", action="store_true",
                   help="在 runtime/selftest/ 跑一遍自检后清理")
    return p


def _db_count(db_path, date_str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT call_count FROM call_sum WHERE date = ?", (date_str,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _export_csv(month: str, result: dict) -> Path:
    cfg = counter.get_config()
    cfg.export_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.export_dir / f"call_stat_{month}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "callCount"])
        for row in result["days"]:
            writer.writerow([row["date"], row["callCount"]])
    print(f"已导出: {path}", flush=True)
    return path


def _self_check() -> int:
    """自检：阈值落库 / 重启恢复 / 月补齐 / 参数校验 / 产物边界。

    临时库固定在 ``runtime/selftest/call_sum.db``（仍在模块目录内），结束即删除。
    """
    probe = RUNTIME_DIR / "selftest"
    if probe.exists():
        shutil.rmtree(probe)
    db = probe / "call_sum.db"
    today = datetime.date.today().strftime("%Y-%m-%d")
    try:
        counter._reset_state_for_test()
        counter.configure(flush_threshold=3, db_path=db)
        assert counter.startup() == 0, "空库启动基数应为 0"

        values = [counter.record_call() for _ in range(7)]
        assert values == [1, 2, 3, 4, 5, 6, 7], values
        assert _db_count(db, today) == 6, "7 次调用 / 阈值 3 应落库 6 次"
        assert counter.get_call_count(today) == 7, "完整累计须含未落库增量"
        assert counter.flush() == {}, "flush 后不应再有 pending"
        assert _db_count(db, today) == 7, "flush 应补齐尾部增量"

        # 模拟进程重启：清空内存后 startup() 必须从库读回 7，而不是从 0 开始
        counter._reset_state_for_test()
        counter.configure(flush_threshold=3, db_path=db)
        assert counter.startup() == 7, "重启后应从库恢复基数"
        assert counter.record_call() == 8
        counter.flush()
        assert _db_count(db, today) == 8

        month = today[:7]
        rows = counter.get_month_counts(month)
        assert len(rows) == month_day_count(month), "月查询须补齐整月每一天"
        assert dict(rows)[today] == 8

        assert validate_params(None, None)[0] is False
        assert validate_params(today, month)[0] is False
        assert validate_params("2026-1-6", None)[0] is False
        assert validate_params(None, "2026-13")[0] is False
        assert validate_params(today, None)[0] is True
        assert db.resolve().is_relative_to(RUNTIME_DIR.resolve()), "产物越出模块目录"

        print("SELF-CHECK OK: 阈值落库 / 重启恢复 / 月补齐 / 参数校验 / 产物边界",
              flush=True)
        return 0
    finally:
        counter._reset_state_for_test()
        if probe.exists():
            shutil.rmtree(probe)


def main(argv=None) -> int:
    """解析参数、执行动作、返回进程退出码（0 正常 / 1 参数错 / 2 运行错）。"""
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        if args.self_check:
            return _self_check()
        if args.flush:
            print(f"flush 后待落库: {counter.flush()}", flush=True)
            return 0
        if not args.date and not args.month:
            parser.print_help()
            return 2
        ok, desc = validate_params(args.date, args.month)
        if not ok:
            print(f"ERROR:: {desc}", flush=True)
            return 1
        if args.date:
            if args.export:
                warn("--export 仅对 --month 生效，本次忽略")
            print(json.dumps(date_result(args.date), ensure_ascii=False), flush=True)
            return 0
        result = month_result(args.month)
        if args.export:
            _export_csv(args.month, result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as e:
        print(f"ERROR:: {e}", flush=True)
        return 2


__all__ = ["build_arg_parser", "main"]
