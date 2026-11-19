# -*- coding: utf-8 -*-
"""写入侧守护编排：单实例锁 + 定时轮询 + 逐表导入 + 日志。

组件全部由构造器注入，``serve_forever`` 本身不建库、不建目录、不装 logger：

    task = build_watcher()                      # 默认：registry.json + fcntl 锁
    rc = task.watcher.serve_forever(max_rounds=2, sleeper=lambda _: None)
    task.close()

``--once`` 即 ``max_rounds=1``；``max_rounds`` / ``sleeper`` / ``logger`` / ``lock``
都是注入点，便于离线断言退出码与轮数，不改变生产默认行为。

流程与日志文本平移自旧 ``dim_watcher``：逐表 ``MissingColumn`` / 其他异常都跳过
本轮、指纹不更新、下轮重试；表与表之间互不影响。
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .config import RegistryLoader
from .excel_source import OpenpyxlExcelSource
from .importer import TableImporter
from .models import Registry
from .protocols import (
    ImportResult, InstanceLock, LogSink, MissingColumn, Sleeper, WritableRepository,
)
from .repository import SqliteDimensionRepository


class SingleInstanceLock:
    """``fcntl`` 非阻塞文件锁，保证单机单实例；失败返回 False 而非抛异常。

    ``fcntl`` 在方法内导入：非 Linux 平台仍可 import 本模块，由宿主项目换锁实现。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.fh = None

    def acquire(self) -> bool:
        import fcntl  # Linux 专用，延迟到真正拿锁时

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

    def release(self) -> None:
        import fcntl

        if self.fh is None:
            return
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None


def build_logger(log_dir: Path, *, logger: LogSink | None = None) -> LogSink:
    """按天轮转（保留 7 天）+ stderr 双出口；注入 logger 时直接返回，不碰磁盘。"""
    if logger is not None:
        return logger
    log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("local_excel2db.watcher")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = TimedRotatingFileHandler(
        log_dir / "dim_watcher.log", when="midnight", backupCount=7,
        encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


class DimensionWatcher:
    """逐表编排 + 守护循环；依赖注入，自身不持有资源所有权。"""

    def __init__(self, registry: Registry, importer: TableImporter, *,
                 lock: InstanceLock, log: LogSink,
                 sleeper: Sleeper | None = None) -> None:
        self.registry = registry
        self.importer = importer
        self.lock = lock
        self.log = log
        self._sleep: Sleeper = sleeper if sleeper is not None else time.sleep

    def run_once(self, *, force: bool = False) -> list[ImportResult]:
        """跑一轮；逐表独立失败互不影响，返回本轮成功产出的结构化结果。"""
        results: list[ImportResult] = []
        for spec in self.registry.tables:
            name = spec.table
            try:
                r = self.importer.import_table(spec, force=force)
            except MissingColumn as e:
                self.log.error(f"[{name}] {e}，本轮跳过，下轮重试")
                continue
            except Exception as e:
                self.log.exception(f"[{name}] 导入失败，指纹未更新，下轮重试: {e}")
                continue

            if r.status == "missing":
                self.log.warning(f"[{name}] 注册文件不存在，跳过: {r.path}")
            elif r.status == "unchanged":
                self.log.info(f"[{name}] 未变化，跳过（当前 version={r.version}）")
            else:
                self.log.info(
                    f"[{name}] version={r.version} 写入 {r.rows} 行 "
                    f"(空主键跳过 {r.skipped_empty}，重复主键覆盖 {r.duplicated}，"
                    f"sha256={r.sha256[:12]}…)")
            results.append(r)
        return results

    def serve_forever(self, *, interval: int | None = None,
                      max_rounds: int | None = None,
                      force: bool = False) -> int:
        """拿锁 → 跑 N 轮（``None`` 为无限）→ 退出释放锁；返回进程退出码。

        最后一轮之后不休眠；``--once`` 用 ``max_rounds=1``。
        """
        seconds = interval if interval and interval > 0 \
            else self.registry.poll_interval_seconds
        if not self.lock.acquire():
            self.log.error("已有 local_excel2db watcher 实例在运行，退出")
            return 1

        try:
            self.log.info(
                f"local_excel2db watcher 启动 pid={os.getpid()} "
                f"间隔={seconds}s 库={self.registry.database} "
                f"注册表={self.registry.config_path} "
                f"注册 {len(self.registry.tables)} 张表 "
                f"max_rounds={max_rounds} force={force}")
            rounds = 0
            while max_rounds is None or rounds < max_rounds:
                try:
                    self.run_once(force=force)
                except Exception as e:
                    self.log.exception(f"本轮检测异常: {e}")
                rounds += 1
                if max_rounds is None or rounds < max_rounds:
                    self._sleep(seconds)
            return 0
        except KeyboardInterrupt:
            self.log.info("收到中断信号，退出")
            return 0
        finally:
            self.lock.release()


@dataclass
class AssembledWatcher:
    """装配完成、可立即运行的写入侧任务；字段公开，便于单测断言与组件替换。"""

    registry: Registry
    repository: WritableRepository
    importer: TableImporter
    lock: InstanceLock
    log: LogSink
    watcher: DimensionWatcher

    def close(self) -> None:
        self.repository.close()


def build_watcher(registry: Registry | None = None, *,
                  config_path: Path | str | None = None,
                  logger: LogSink | None = None,
                  lock: InstanceLock | None = None,
                  sleeper: Sleeper | None = None,
                  connection_factory=None,
                  log_dir: Path | None = None) -> AssembledWatcher:
    """唯一的装配入口；所有可选参数都是注入点，缺省即生产默认。

    ``config_path`` 决定 registry，也就决定库 / xlsx / 锁 / 日志的目录基准：
    全部相对 registry 文件所在目录，其他项目不必把文件放进包目录。
    """
    reg = registry if registry is not None else RegistryLoader().from_file(config_path)
    repo = SqliteDimensionRepository(reg, connection_factory=connection_factory)
    importer = TableImporter(reg, OpenpyxlExcelSource(), repo)
    lk = lock if lock is not None else SingleInstanceLock(
        reg.base_dir / "state" / "dim_watcher.lock")
    log = build_logger(log_dir or reg.base_dir / "log", logger=logger)
    watcher = DimensionWatcher(reg, importer, lock=lk, log=log, sleeper=sleeper)
    return AssembledWatcher(reg, repo, importer, lk, log, watcher)


__all__ = ["SingleInstanceLock", "build_logger", "DimensionWatcher",
           "AssembledWatcher", "build_watcher"]
