# -*- coding: utf-8 -*-
"""命令行入口：``python -m local_excel2db [--once] [--force]``。

配置与路径都固定在包内（见 ``config.py``）：唯一配置来源是
``local_excel2db/registry.json``，输入恒为项目根 ``excels/``，库 / 锁 / 日志
恒落在 ``local_excel2db/runtime/``。CLI 不提供 ``--config`` / ``--interval`` /
``--runtime-dir`` 等覆盖入口，改轮询间隔或列映射只改包内 registry.json 并重启。

参数与退出码：

    --once              只检测一轮后退出（crontab / 验证）
    --force             忽略指纹强制重导并递增 version
    锁冲突 → 1；初始化失败（配置损坏/缺失）→ 2；正常 → 0

``main()`` 返回 int 而不调用 ``sys.exit``，方便单测直接断言退出码；真正的进程
退出在 ``__main__.py`` 里做。``logger`` / ``lock`` / ``sleeper`` /
``connection_factory`` 均为离线运行与注入测试用的可选参数，只替换组件、不改路径。
"""
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
    """解析参数、装配写入侧任务、跑守护循环；返回进程退出码。"""
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
