"""命令行入口 ``python -m sftp_avl2db --task <任务名> [选项]``。"""
from __future__ import annotations

import argparse
from datetime import datetime

from .avl_specs import AVL_SPECS
from .config import load_profiles


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m sftp_avl2db",
        description="SFTP AVL → SQLite 定时拉取与入库")
    p.add_argument("--task", required=True,
                   help=f"AVL 名（{' / '.join(AVL_SPECS)}）；一个 AVL 一个 db")
    p.add_argument("--date", default=None,
                   help="单次模式：只拉取指定日期 YYYYMMDD 后退出；不传则常驻轮询")
    p.add_argument("--force-reload", action="store_true",
                   help="本地已存在 / 已在清单中的文件也强制重新下载并入库")
    p.add_argument("--runtime-dir", default=None,
                   help="运行时产出目录（默认包内 runtime/；离线复跑可指到临时目录）")
    p.add_argument("--local-dir", default=None,
                   help="离线源目录：把该目录当远程，不连 SFTP")
    p.add_argument("--max-rounds", type=int, default=None,
                   help="常驻模式最多运行轮数（离线测试用）")
    return p


def main(argv=None, *, profiles=None, logger=None, sleeper=None, now=None) -> int:
    """解析参数、装配任务、跑守护循环。"""
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
            profiles = load_profiles(runtime_dir=args.runtime_dir)
        if args.task not in profiles:
            print(f"ERROR:: 未知 AVL {args.task}；可选：{', '.join(sorted(profiles))}",
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
