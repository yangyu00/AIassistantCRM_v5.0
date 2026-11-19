# -*- coding: utf-8 -*-
"""``Avl2DbRunner``：把五个可注入组件编成一轮「SFTP AVL → SQLite」。

复刻 ``tasks/base_task.py`` 的 ``run_once_for_date()`` / ``serve_forever()`` /
``cleanup_expired()`` 的**执行顺序与日志语义**，逐句对照见下方注释。

两个经审核的接线决策（阶段 5 用户确认按推荐执行）：

1. **未知前缀当作失败**：旧 ``OpportunityRecallTask.parse_and_save()`` 识别不到
   前缀时 ``log.error + return``，上层不感知、仍会 ``manifest.add()`` +
   ``imported += 1``（静默丢数据）。新 ``PerPrefixLayout`` 抛
   ``UnknownPrefixError``，这里统一按「解析入库失败」处理：**不登记清单、
   imported 不加、本地文件保留、下轮重试**。该分支实际不可达（上层只下发按
   ``FILE_PREFIXES`` 拼出的文件名），不影响 golden 基线。
2. **``exists()`` 三态沿用旧日志映射**：``False`` → INFO「远程文件尚未生成」；
   其他异常 → ERROR「stat 远程文件出错」；两者都 continue、不登记清单。

**日志信号保真**：解析完成后按任务补记旧子类里那两类计数文本
（``共 N 条`` / ``跳过空手机号 N 条``），保证 ``golden.json`` 的
``log_signals`` 逐项对齐。逐批进度日志（如「已覆盖入库 N 条」「表 T 写入 N 条」）
收敛为 ``WriteStats``，组件本身不写日志。

**测试钩子**（不改变生产行为）：``max_rounds`` 限制常驻轮数、``sleeper`` 可注入
假 sleep、``now`` / ``date_strings`` 可注入时钟，让守护循环也能离线单测。
"""
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from .protocols import ImportResult


class Avl2DbRunner:
    """单任务编排器；全部依赖由构造器注入，任何一层都能换成测试替身。"""

    def __init__(self, profile, *, source, parser, sink, layout,
                 state, lock, log, sleeper=None, now=None):
        self.profile = profile
        self.source = source
        self.parser = parser
        self.sink = sink
        self.layout = layout
        self.state = state
        self.lock = lock
        self.log = log
        self._sleep = sleeper if sleeper is not None else time.sleep
        # ``now`` 可传可调用对象（默认 datetime.now），也可直接传一个固定
        # datetime 便于单测锁定 cleanup cutoff；统一规范成可调用。
        raw_now = now if now is not None else datetime.now
        self._now = raw_now if callable(raw_now) else (lambda: raw_now)

    # ---- 路径与命名（与旧 _remote_name / run_once_for_date 一致） ----
    def _remote_path(self, remote_name: str) -> str:
        root = self.profile.sftp.remote_path.rstrip("/")
        return f"{root}/{remote_name}"

    def _local_path(self, remote_name: str) -> Path:
        return self.profile.local_dir / remote_name

    # ---- 单个远程文件 ----
    def import_one(self, date_str: str, prefix: str,
                   force: bool = False) -> ImportResult:
        """处理一个前缀在这一天的文件，返回结构化结果。

        顺序逐句复刻旧 ``run_once_for_date`` 的 for 循环体：
        拼名 → 清单命中且非 force 则 SKIP → stat → get → parse+write →
        **成功后才** ``state.add()`` → imported += 1。

        生产上由 ``run_once_for_date()`` 调用，后者已先 ``sink.ensure_schema()``；
        单独调用本方法（例如离线单测）需自行确保表已建好。
        """
        remote_name = self.profile.remote_name_tmpl.format(
            prefix=prefix, date=date_str)
        remote_path = self._remote_path(remote_name)
        local_path = self._local_path(remote_name)

        if self.state.has(remote_name) and not force:
            self.log.info(f"SKIP 已在清单中，跳过: {remote_name}")
            return ImportResult(remote_name, "skip", detail="已在清单中")

        # 旧代码：try sftp.stat(...) / except FileNotFoundError / except Exception
        try:
            present = self.source.exists(remote_path)
        except FileNotFoundError:
            present = False
        except Exception as e:
            self.log.error(f"stat 远程文件出错 {remote_name}: {e}")
            return ImportResult(remote_name, "failed", detail=f"stat: {e}")
        if not present:
            self.log.info(f"远程文件尚未生成: {remote_name}")
            return ImportResult(remote_name, "missing")

        try:
            self.log.info(f"正在下载 {remote_name} ...")
            self.source.fetch(remote_path, local_path)
        except Exception as e:
            self.log.error(f"下载失败 {remote_name}: {e}")
            return ImportResult(remote_name, "failed", detail=f"get: {e}")

        try:
            # 解析层是生成器；写入层按 BATCH_SIZE 分桶、逐批 commit
            rows = self.parser.parse(local_path)
            self.sink.write(remote_name, rows, router=self.layout)
            self._log_import_done(remote_name, prefix)
            # ↑ 成功后才登记清单；下面任何异常都不 add，下轮会重试
            self.state.add(remote_name)
            self.log.info(f"入库完成并登记清单: {remote_name}")
            return ImportResult(remote_name, "ok",
                                rows=self.parser.stats.rows,
                                skipped=self.parser.stats.skipped)
        except Exception as e:
            # 含 UnknownPrefixError：按失败处理，不登记清单（接线决策 1）
            self.log.error(
                f"解析入库失败 {remote_name}: {e}\n{traceback.format_exc()}")
            return ImportResult(remote_name, "failed", detail=f"import: {e}")

    def _log_import_done(self, filename: str, prefix: str) -> None:
        """按旧子类的文本补记两类计数，保证 golden.log_signals 对齐。"""
        st = self.parser.stats
        if self.profile.routing == "per_prefix":
            self.log.info(
                f"[{prefix}] {filename} 覆盖完成，共 {st.rows} 条，"
                f"跳过空手机号 {st.skipped} 条")
        else:
            self.log.info(
                f"{filename} 解析完成，共 {st.rows} 条，"
                f"跳过空手机号 {st.skipped} 条")

    # ---- 单轮 ----
    def run_once_for_date(self, date_str: str,
                          force_reload: bool = False) -> int:
        """返回本轮真正新入库的文件数（旧方法同名同返回值）。"""
        self.profile.local_dir.mkdir(parents=True, exist_ok=True)
        self.sink.ensure_schema()          # 旧 init_db()，幂等

        # 旧代码：连接失败 → log.error → return 0（整轮不处理任何文件）
        try:
            self.source.connect()
            self.log.info("SFTP 连接成功")
        except Exception as e:
            self.log.error(f"SFTP 连接失败: {e}")
            return 0

        imported = 0
        try:
            for prefix in self.profile.file_prefixes:
                result = self.import_one(date_str, prefix, force=force_reload)
                if result.status == "ok":
                    imported += 1
        finally:
            # 旧 finally：先关 sftp 再关 ssh，异常吞掉
            try:
                self.source.close()
            except Exception:
                pass
        return imported

    # ---- 过期清理 ----
    def cleanup_expired(self) -> dict:
        """复刻旧 ``cleanup_expired()`` = 本地 AVL 清理 + 库表过期数据清理。"""
        removed, errors = [], []

        # 旧 _cleanup_local_files()：按 mtime 删 .AVL，异常记 ERROR 后继续
        local_dir = self.profile.local_dir
        expire = self._now() - timedelta(
            days=self.profile.retention.local_file_days)
        if local_dir.is_dir():
            for name in sorted(os.listdir(local_dir)):
                if not name.endswith(".AVL"):
                    continue
                fp = local_dir / name
                try:
                    if datetime.fromtimestamp(fp.stat().st_mtime) < expire:
                        fp.unlink()
                        removed.append(name)
                        self.log.info(f"已删除过期本地文件: {name}")
                except Exception as e:
                    errors.append(f"{name}: {e}")
                    self.log.error(f"删除过期本地文件失败 {name}: {e}")

        # 旧 _cleanup_db()：cutoff = (now - DATA_EXPIRE_DAYS).strftime("%Y-%m-%d")
        cutoff = (self._now() - timedelta(
            days=self.profile.retention.data_days)).strftime("%Y-%m-%d")
        purged = self.sink.purge_expired(cutoff)
        for db_path, msg in self.sink.stats.errors:
            errors.append(f"{db_path}: {msg}")
            self.log.error(f"清理过期数据失败 {db_path}: {msg}")
        if purged > 0:
            self.log.info(f"清理过期数据 {purged} 条 (op_time < {cutoff})")

        return {"removed_local_files": removed, "purged_rows": purged,
                "cutoff": cutoff, "errors": errors}

    # ---- 守护循环 ----
    def serve_forever(self, only_date: str | None = None,
                      force_reload: bool = False,
                      max_rounds: int | None = None,
                      date_strings=None) -> int:
        """拿锁 → 单次 / 常驻 → 每轮清理 → sleep → 退出释放锁。

        ``max_rounds`` / ``date_strings`` 仅用于离线测试；生产传默认（无限、
        按「昨天 + 今天」兜底）与旧代码完全一致。
        """
        if not self.lock.acquire():
            self.log.error(
                f"获取任务锁失败，已有 {self.profile.name} 实例在运行，退出。")
            return 1
        try:
            self.profile.local_dir.mkdir(parents=True, exist_ok=True)
            self.sink.ensure_schema()
            self.log.info(
                f"{self.profile.name} 守护进程启动 pid={os.getpid()} "
                f"轮询间隔={self.profile.retention.poll_seconds}s "
                f"前缀={list(self.profile.file_prefixes)}")

            if only_date:
                n = self.run_once_for_date(only_date,
                                           force_reload=force_reload)
                self.cleanup_expired()
                self.log.info(f"单次模式结束，本次新入库 {n} 个文件")
                return 0

            rounds = 0
            while max_rounds is None or rounds < max_rounds:
                if date_strings is not None:
                    dates = list(date_strings)
                else:
                    now = self._now()
                    today = now.strftime("%Y%m%d")
                    # 每轮同时兜底前一天，防止跨天瞬间漏拉
                    dates = [(now - timedelta(days=1)).strftime("%Y%m%d"),
                             today]
                total = 0
                for d in dict.fromkeys(dates):
                    total += self.run_once_for_date(d,
                                                    force_reload=force_reload)
                self.cleanup_expired()
                self.log.info(
                    f"本轮结束，新入库 {total} 个文件，休眠 "
                    f"{self.profile.retention.poll_seconds}s")
                rounds += 1
                self._sleep(self.profile.retention.poll_seconds)
            return 0
        except KeyboardInterrupt:
            self.log.info("收到中断信号，退出。")
            return 0
        except Exception as e:
            self.log.error(f"守护循环异常退出: {e}\n{traceback.format_exc()}")
            return 1
        finally:
            self.lock.release()


__all__ = ["Avl2DbRunner"]
