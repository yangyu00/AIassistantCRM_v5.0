"""``Avl2DbRunner``：把五个可注入组件编成一轮「SFTP AVL → SQLite」。"""
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from .config import remote_name_for
from .protocols import ImportResult, ImportStatus


class Avl2DbRunner:
    """单任务编排器。"""

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
        # 库表清理「每天一次」的进程内闸门：只记日历序号，不落状态文件，
        # 同一天最多触发一次清理（进程重启可能重复一次）。
        self._last_purge_day: int | None = None

    def _remote_path(self, remote_name: str) -> str:
        root = self.profile.sftp.remote_path.rstrip("/")
        return f"{root}/{remote_name}"

    def _local_path(self, remote_name: str) -> Path:
        return self.profile.local_dir / remote_name

    def import_one(self, date_str: str, prefix: str,
                   force: bool = False) -> ImportResult:
        """处理一个前缀在这一天的文件，返回结构化结果。"""
        remote_name = remote_name_for(self.profile, prefix, date_str)
        remote_path = self._remote_path(remote_name)
        local_path = self._local_path(remote_name)

        if self.state.has(remote_name) and not force:
            self.log.info(f"SKIP 已在清单中，跳过: {remote_name}")
            return ImportResult(remote_name, ImportStatus.SKIP, detail="已在清单中")

        # stat 三态：存在 / 尚未生成 / 其他错误
        try:
            present = self.source.exists(remote_path)
        except FileNotFoundError:
            present = False
        except Exception as e:
            self.log.error(f"stat 远程文件出错 {remote_name}: {e}")
            return ImportResult(remote_name, ImportStatus.FAILED, detail=f"stat: {e}")
        if not present:
            self.log.info(f"远程文件尚未生成: {remote_name}")
            return ImportResult(remote_name, ImportStatus.MISSING)

        try:
            self.log.info(f"正在下载 {remote_name} ...")
            self.source.fetch(remote_path, local_path)
        except Exception as e:
            self.log.error(f"下载失败 {remote_name}: {e}")
            return ImportResult(remote_name, ImportStatus.FAILED, detail=f"get: {e}")

        try:
            # 解析层是生成器；写入层按 BATCH_SIZE 分桶、逐批 commit
            rows = self.parser.parse(local_path)
            self.sink.write(remote_name, rows, layout=self.layout)
            self._log_import_done(remote_name, prefix)
            # ↑ 成功后才登记清单；下面任何异常都不 add，下轮会重试
            self.state.add(remote_name)
            self.log.info(f"入库完成并登记清单: {remote_name}")
            # rows 取解析产出的逻辑记录数；正常情况下等于 sink.write() 的写入数。
            return ImportResult(remote_name, ImportStatus.OK,
                                rows=self.parser.stats.rows,
                                skipped=self.parser.stats.skipped)
        except Exception as e:
            # 解析 / 写入异常：按失败处理，不登记清单，下轮重试
            self.log.error(
                f"解析入库失败 {remote_name}: {e}\n{traceback.format_exc()}")
            return ImportResult(remote_name, ImportStatus.FAILED, detail=f"import: {e}")

    def _log_import_done(self, filename: str, prefix: str) -> None:
        """单文件入库完成计数。"""
        st = self.parser.stats
        self.log.info(
            f"[{prefix}] {filename} 覆盖完成，共 {st.rows} 条，"
            f"跳过空主键 {st.skipped} 条")

    def run_once_for_date(self, date_str: str,
                          force_reload: bool = False) -> int:
        """返回本轮真正新入库的文件数。"""
        self.profile.local_dir.mkdir(parents=True, exist_ok=True)
        self.sink.ensure_schema()          # 幂等

        # 连接失败 → 整轮不处理任何文件
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
                if result.status == ImportStatus.OK:
                    imported += 1
        finally:
            # 无论成功失败都关闭连接，异常吞掉
            try:
                self.source.close()
            except Exception:
                pass
        return imported

    def _purge_plan(self, day_ordinal: int, cycle: int):
        """算出当天要清的表：``(tables, 总表数, 生效周期)``。"""
        all_tables = [t for _, t in self.layout.all_targets()]
        if cycle <= 0:
            return None, len(all_tables), 0
        effective = min(cycle, len(all_tables))
        if not getattr(self.profile, "sharded", False) or len(all_tables) <= 1:
            return all_tables, len(all_tables), effective
        picked = [t for i, t in enumerate(all_tables)
                  if i % effective == day_ordinal % effective]
        return picked, len(all_tables), effective

    def cleanup_expired(self, *, force_full: bool = False) -> dict:
        """本地 AVL 清理（每轮）+ 库表过期数据清理（按轮转周期每天一次）。"""
        removed, errors = [], []

        # 按 mtime 删过期 .AVL，异常记 ERROR 后继续；这一步仍每轮执行
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

        # cutoff = (now - DATA_EXPIRE_DAYS)
        cutoff = (self._now() - timedelta(
            days=self.profile.retention.data_days)).strftime("%Y-%m-%d")
        cycle = getattr(self.profile.retention, "purge_cycle_days", 0)
        day = self._now().date().toordinal()
        tables, total_tables, effective = self._purge_plan(day, cycle)

        if cycle <= 0 or force_full:
            should_purge, tables = True, None
        elif self._last_purge_day == day:
            should_purge, tables = False, []
        else:
            should_purge = True
            self._last_purge_day = day

        purged = 0
        purged_tables = 0
        if should_purge:
            purged = self.sink.purge_expired(cutoff, tables)
            purged_tables = total_tables if tables is None else len(tables)
            for db_path, msg in self.sink.stats.errors:
                errors.append(f"{db_path}: {msg}")
                self.log.error(f"清理过期数据失败 {db_path}: {msg}")
            if purged > 0:
                self.log.info(f"清理过期数据 {purged} 条 (op_time < {cutoff})")
            if force_full:
                self.log.info("单次补跑：跳过轮转，本次全量清理库表")
            elif cycle > 0:
                self.log.info(
                    f"库表清理轮转 cycle={effective}天 本次 "
                    f"{purged_tables}/{total_tables} 张表 (op_time < {cutoff})")

        return {"removed_local_files": removed, "purged_rows": purged,
                "cutoff": cutoff, "purged_tables": purged_tables,
                "purge_skipped": not should_purge, "errors": errors}

    def serve_forever(self, only_date: str | None = None,
                      force_reload: bool = False,
                      max_rounds: int | None = None,
                      date_strings=None) -> int:
        """拿锁 → 单次 / 常驻 → 每轮本地清理 + 按周期库表清理 → sleep → 退出释放锁。"""
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
                self.cleanup_expired(force_full=True)
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
                for d in sorted(set(dates)):
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
