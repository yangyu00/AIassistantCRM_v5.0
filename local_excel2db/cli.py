# -*- coding: utf-8 -*-
"""命令行入口：``python -m local_excel2db --once [--force]``。

参数与退出码：

    --config PATH       指定 registry.json（决定库 / xlsx / 锁 / 日志的目录基准）
    --interval N        覆盖轮询间隔（秒）
    --once              只检测一轮后退出（crontab / 验证）
    --force             忽略指纹强制重导（通常配合 --once）
    锁冲突 → 1；初始化失败（配置损坏/缺失）→ 2；正常 → 0

``main()`` 返回 int 而不调用 ``sys.exit``，方便单测直接断言退出码；真正的进程
退出在 ``__main__.py`` 里做。``registry`` / ``logger`` / ``sleeper`` / ``lock`` /
``connection_factory`` 均为离线运行与注入测试用的可选参数，不改变生产默认。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .protocols import InstanceLock, LogSink, Sleeper
from .models import Registry


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m local_excel2db",
        description="本地 Excel 维表 → SQLite 变更检测与写入")
    p.add_argument("--config", default=None, help="指定 registry.json 路径")
    p.add_argument("--interval", type=int, default=None, help="覆盖轮询间隔（秒）")
    p.add_argument("--once", action="store_true", help="只检测一轮后退出")
    p.add_argument("--force", action="store_true", help="忽略指纹强制重导")
    return p


def main(argv=None, *, registry: Registry | None = None,
         logger: LogSink | None = None, lock: InstanceLock | None = None,
         sleeper: Sleeper | None = None, connection_factory=None) -> int:
    """解析参数、装配写入侧任务、跑守护循环；返回进程退出码。"""
    args = build_arg_parser().parse_args(argv)
    try:
        from .watcher import build_watcher
        task = build_watcher(
            registry=registry, config_path=args.config, logger=logger,
            lock=lock, sleeper=sleeper, connection_factory=connection_factory)
    except (OSError, ValueError) as e:
        print(f"ERROR:: 初始化失败: {e}", flush=True)
        return 2

    try:
        return task.watcher.serve_forever(
            interval=args.interval,
            max_rounds=1 if args.once else None,
            force=args.force,
        )
    finally:
        task.close()


__all__ = ["build_arg_parser", "main"]
