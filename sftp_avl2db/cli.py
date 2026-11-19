# -*- coding: utf-8 -*-
"""命令行入口：``python -m sftp_avl2db --task <任务名> [选项]``。

沿用旧 ``tasks/task_xxx.py`` 的两个参数与退出码语义：

    --date YYYYMMDD          单次模式，拉完指定日期后退出；不传则常驻轮询
    --force-reload           清单命中也强制重新下载并入库
    日期格式错误 → 打印 "ERROR:: 日期格式错误，请使用 YYYYMMDD"，退出码 2
    锁冲突       → 退出码 1（由 ``serve_forever`` 返回）
    正常         → 0

新增的注入型选项（仅用于离线运行 / 单测，不改变生产默认）：

    --local-dir DIR          用本地目录当远程源，不连 SFTP、不需要 paramiko
    --env PATH / --project-root PATH
    --max-rounds N          常驻模式最多跑 N 轮后退出

``main()`` **返回 int 而不调用 ``sys.exit``**，方便单测直接断言退出码；
真正的进程退出在 ``__main__.py`` 里做。
"""
from __future__ import annotations

import argparse
from datetime import datetime

from .config import load_profiles


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m sftp_avl2db",
        description="SFTP AVL → SQLite 定时拉取与入库")
    p.add_argument("--task", required=True,
                   help="任务名（opportunity_recall / loss_control）")
    p.add_argument("--date", default=None,
                   help="单次模式：只拉取指定日期 YYYYMMDD 后退出；不传则常驻轮询")
    p.add_argument("--force-reload", action="store_true",
                   help="本地已存在 / 已在清单中的文件也强制重新下载并入库")
    p.add_argument("--env", default=None, help="指定 .env 路径（覆盖 TASKS_ENV_FILE）")
    p.add_argument("--project-root", default=None, help="项目根目录（配置路径派生用）")
    p.add_argument("--local-dir", default=None,
                   help="离线源目录：把该目录当远程，不连 SFTP")
    p.add_argument("--max-rounds", type=int, default=None,
                   help="常驻模式最多运行轮数（离线测试用）")
    return p


def main(argv=None, *, profiles=None, logger=None, sleeper=None, now=None) -> int:
    """解析参数、装配任务、跑守护循环；返回进程退出码。"""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.date:
        try:
            datetime.strptime(args.date, "%Y%m%d")
        except ValueError:
            print("ERROR:: 日期格式错误，请使用 YYYYMMDD", flush=True)
            return 2

    try:
        if profiles is None:
            profiles = load_profiles(env_path=args.env,
                                     project_root=args.project_root)
        if args.task not in profiles:
            print(f"ERROR:: 未知任务 {args.task}；可选：{', '.join(sorted(profiles))}",
                  flush=True)
            return 2
        from .profiles import build_task
        task = build_task(profiles[args.task], logger=logger,
                          local_dir=args.local_dir,
                          sleeper=sleeper, now=now)
    except Exception as e:
        print(f"ERROR:: 初始化失败: {e}", flush=True)
        return 2

    try:
        return task.runner.serve_forever(
            only_date=args.date,
            force_reload=args.force_reload,
            max_rounds=args.max_rounds,
        )
    finally:
        task.close()


__all__ = ["build_arg_parser", "main"]
