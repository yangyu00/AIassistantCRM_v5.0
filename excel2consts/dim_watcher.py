# -*- coding: utf-8 -*-
"""维表独立进程：定时检测注册文件是否更新，按配置抽列写入 SQLite。

运行（项目根目录）：
    python -m excel2consts.dim_watcher
    python -m excel2consts.dim_watcher --once
    python -m excel2consts.dim_watcher --once --force
"""
import argparse
import fcntl
import logging
import os
import sqlite3
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .dim_config import BASE_DIR, load_registry
from .dim_importer import MissingColumn, import_file


class SingleInstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.fh = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w", encoding="utf-8")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        self.fh.write(f"pid={os.getpid()}\n")
        self.fh.flush()
        return True

    def release(self):
        if self.fh is None:
            return
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None


def setup_logger():
    log_dir = BASE_DIR / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dim_watcher")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = TimedRotatingFileHandler(
        log_dir / "dim_watcher.log", when="midnight", backupCount=7, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def run_once(conn, cfg, log, force=False):
    for spec in cfg["tables"]:
        name = spec["table"]
        try:
            r = import_file(conn, cfg, spec, force=force)
        except MissingColumn as e:
            log.error(f"[{name}] {e}，本轮跳过，下轮重试")
            continue
        except Exception as e:
            log.exception(f"[{name}] 导入失败，指纹未更新，下轮重试: {e}")
            continue

        if r["status"] == "missing":
            log.warning(f"[{name}] 注册文件不存在，跳过: {r['path']}")
        elif r["status"] == "unchanged":
            log.info(f"[{name}] 未变化，跳过（当前 version={r['version']}）")
        else:
            log.info(
                f"[{name}] version={r['version']} 写入 {r['rows']} 行 "
                f"(空主键跳过 {r['skipped_empty']}，重复主键覆盖 {r['duplicated']}，sha256={r['sha256']}…)"
            )


def main(argv=None):
    ap = argparse.ArgumentParser(description="维表文件变更检测与 SQLite 写入独立进程")
    ap.add_argument("--config", default=None, help="指定 registry.json 路径")
    ap.add_argument("--interval", type=int, default=None, help="覆盖轮询间隔（秒）")
    ap.add_argument("--once", action="store_true", help="只检测一轮后退出")
    ap.add_argument("--force", action="store_true", help="忽略指纹强制重导（通常配合 --once）")
    args = ap.parse_args(argv)

    cfg = load_registry(args.config)
    log = setup_logger()
    interval = args.interval if args.interval and args.interval > 0 else cfg["poll_interval_seconds"]

    lock = SingleInstanceLock(BASE_DIR / "state" / "dim_watcher.lock")
    if not lock.acquire():
        log.error("已有 dim_watcher 实例在运行，退出")
        return 1

    cfg["database"].parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cfg["database"]))
    log.info(
        f"dim_watcher 启动 pid={os.getpid()} "
        f"间隔={interval}s 库={cfg['database']} 注册表={cfg['config_path']} "
        f"注册 {len(cfg['tables'])} 张表 once={args.once} force={args.force}"
    )
    try:
        while True:
            try:
                run_once(conn, cfg, log, force=args.force)
            except Exception as e:
                log.exception(f"本轮检测异常: {e}")
            if args.once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("收到中断信号，退出")
        return 0
    finally:
        conn.close()
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
