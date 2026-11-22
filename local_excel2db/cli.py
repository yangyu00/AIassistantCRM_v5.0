# -*- coding: utf-8 -*-
"""命令行入口：``python -m local_excel2db [--once] [--force]``。"""
from __future__ import annotations

import argparse

from .protocols import InstanceLock, LogSink, Sleeper


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m local_excel2db",
        description="本地 Excel 维表 → SQLite 定时变更检测与写入")
    p.add_argument("--once", action="store_true", help="只检测一轮后退出")
    p.add_argument("--force", action="store_true", help="忽略指纹强制重导")
    return p


def main(argv=None, *, logger: LogSink | None = None,
         lock: InstanceLock | None = None, sleeper: Sleeper | None = None,
         connection_factory=None) -> int:
    """解析参数、装配写入侧任务、跑守护循环。"""
    args = build_arg_parser().parse_args(argv)
    try:
        from .watcher import build_watcher
        task = build_watcher(
            logger=logger, lock=lock, sleeper=sleeper,
            connection_factory=connection_factory)
    except (OSError, ValueError) as e:
        print(f"ERROR:: 初始化失败: {e}", flush=True)
        return 2

    try:
        return task.watcher.serve_forever(
            max_rounds=1 if args.once else None,
            force=args.force,
        )
    finally:
        task.close()


__all__ = ["build_arg_parser", "main"]
