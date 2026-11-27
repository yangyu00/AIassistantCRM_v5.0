"""``Avl2DbRunner``：把五个可注入组件编成一轮「SFTP AVL → SQLite」。"""
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from .config import remote_name_for
from .protocols import ImportResult, ImportStatus

# 缺文件兜底只作用于控损（F09591 / project=loss_control），其余 AVL 行为不变。
_LOSS_CONTROL_PROJECT = "loss_control"

# ---- P0：全量替换（DROP）AVL 的失败重试收敛 ---------------------------
# 8000 万行/天全量 AVL 每重试一次 = 再 DROP + 重灌一遍约 8.4GB。常驻轮询
# 每 15 分钟一轮、每轮都检查「昨天 + 今天」，失败时若不收敛，同一文件一天
# 会被反复重灌近百次。连续失败达到阈值即隔离该文件、停止自动重试并 ERROR
# 告警，改为人工确认后用 --force-reload 重跑。
# 只作用于 drop_before_insert=1 的 AVL；商机 / 控损（含断供兜底）行为不变。
DROP_FAILURE_LIMIT = 2
_DROP_QUARANTINE_SUFFIX = ".drop_quarantine"


class Avl2DbRunner:
    """单任务编排器。"""

    def __init__(self, profile, *, source, parser, sink, layout,
                 state, lock, log, sleeper=None, now=None,
                 drop_failure_limit: int = DROP_FAILURE_LIMIT):
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
        # 控损缺文件兜底：同一自然日最多把 op_time 后移一次，避免常驻轮询
        # 把日期反复往后推；另落一个标记文件，进程重启也不会重复推。
        self._last_roll_day: str | None = None
        # P0：DROP 模式失败收敛的进程内计数与持久化隔离集合（懒加载）。
        self.drop_failure_limit = max(1, int(drop_failure_limit))
        self._drop_fail_counts: dict[str, int] = {}
        self._drop_quarantine: set[str] | None = None

    # ---- P0：全量替换模式的失败收敛与清理跳过 -------------------------

    def _drop_before_insert(self) -> bool:
        """该 AVL 是否开启全量替换（DROP）模式；口径与 import_one() 一致。

        读 profile.retention 而不是 sink 方法：测试替身 / 其它 sink 实现
        同样生效，且不会因 sink 缺少该方法而退化成「不收敛」。
        """
        retention = getattr(self.profile, "retention", None)
        return int(getattr(retention, "drop_before_insert", 0) or 0) == 1

    def _quarantine_path(self) -> Path | None:
        """持久化隔离标记：重启进程也不会重新全量重灌已隔离文件。"""
        state_dir = getattr(self.profile, "state_dir", None)
        if state_dir is None:
            return None
        return Path(state_dir) / f"{self.profile.name}{_DROP_QUARANTINE_SUFFIX}"

    def _quarantine(self) -> set[str]:
        """懒加载隔离文件名集合；标记文件损坏时按空集合继续（不阻断服务）。"""
        if self._drop_quarantine is None:
            path = self._quarantine_path()
            items: set[str] = set()
            if path is not None and path.is_file():
                try:
                    items = {
                        line.strip()
                        for line in path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    }
                except OSError:
                    items = set()
            self._drop_quarantine = items
        return self._drop_quarantine

    def _write_quarantine(self) -> None:
        path = self._quarantine_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            body = "".join(f"{name}\n" for name in sorted(self._quarantine()))
            path.write_text(body, encoding="utf-8")
        except OSError as e:
            self.log.error(f"写入 DROP 失败隔离标记失败 {path}: {e}")

    def _track_drop_failure(self, result: ImportResult, force: bool) -> None:
        """按远程文件累计连续失败；达到阈值即隔离，停止自动重试。

        - 只对 ``drop_before_insert=1`` 且非控损的 AVL 生效；
        - MISSING（上游文件尚未生成）/ SKIP 不算失败，不累计；
        - 成功入库或人工 ``--force-reload`` 会清零计数并解除隔离；
        - 控损（F09591）的断供兜底完全不参与，保持原有无限重试语义。
        """
        if not self._drop_before_insert() or self._is_loss_control():
            return
        name = result.filename

        if force or result.status == ImportStatus.OK:
            self._drop_fail_counts.pop(name, None)
            q = self._quarantine()
            if name in q:
                q.discard(name)
                self._write_quarantine()
                self.log.info(f"[{self.profile.name}] 解除失败隔离: {name}")
            return

        if result.status != ImportStatus.FAILED:
            return

        count = self._drop_fail_counts.get(name, 0) + 1
        self._drop_fail_counts[name] = count
        if count < self.drop_failure_limit:
            self.log.warning(
                f"[{self.profile.name}] 全量替换入库失败 "
                f"{count}/{self.drop_failure_limit}: {name} "
                f"（{result.detail}）；下轮仍会重试，达到上限后停止自动重试")
            return

        q = self._quarantine()
        if name in q:
            return
        q.add(name)
        self._write_quarantine()
        self.log.error(
            f"[{self.profile.name}] 全量替换 AVL 连续失败 {count} 次，"
            f"已停止自动重试并隔离: {name}（{result.detail}）。"
            "8000 万行/天全量重灌代价极高，请确认上游文件与磁盘空间后人工介入："
            f"删除 {self._quarantine_path()} 中的该行，或用 --force-reload 重跑；"
            "在此之前该文件不会再被拉取入库")

    def _remote_path(self, remote_name: str) -> str:
        root = self.profile.sftp.remote_path.rstrip("/")
        return f"{root}/{remote_name}"

    def _local_path(self, remote_name: str) -> Path:
        return self.profile.local_dir / remote_name

    def import_one(self, date_str: str, prefix: str,
                   force: bool = False) -> ImportResult:
        """处理一个前缀在这一天的文件，返回结构化结果。

        四段流水线：预检 → 取文件 → 解析入库 → 登记。前三段任一返回
        ``ImportResult`` 即短路；只有写库成功后才登记 ``fetched`` 清单。
        """
        remote_name = remote_name_for(self.profile, prefix, date_str)
        local_path = self._local_path(remote_name)

        skipped = self._precheck(remote_name, force)
        if skipped is not None:
            return skipped
        fetched = self._fetch(remote_name, self._remote_path(remote_name),
                              local_path)
        if fetched is not None:
            return fetched
        return self._import_local(remote_name, prefix, local_path)

    # ---- ① 预检：清单 / 失败隔离 --------------------------------------

    def _precheck(self, remote_name: str,
                  force: bool) -> ImportResult | None:
        """命中即短路返回 SKIP；None 表示可以继续下载。"""
        if self.state.has(remote_name) and not force:
            self.log.info(f"SKIP 已在清单中，跳过: {remote_name}")
            return ImportResult(remote_name, ImportStatus.SKIP,
                                detail="已在清单中")

        # P0：DROP 全量替换 AVL 的失败隔离闸门。命中即停，不再下载 / 解析 /
        # 入库，避免 8000 万行文件被常驻轮询反复重灌。人工 --force-reload
        # 或删除隔离标记后才重新尝试；控损（断供兜底）不参与。
        if (self._drop_before_insert() and not self._is_loss_control()
                and not force and remote_name in self._quarantine()):
            self.log.info(
                f"SKIP 已被失败隔离（连续失败 {self.drop_failure_limit} 次），"
                f"停止自动重试: {remote_name}；确认上游文件修复后，删除 "
                f"{self._quarantine_path()} 中该行或用 --force-reload 重跑")
            return ImportResult(remote_name, ImportStatus.SKIP,
                                detail="DROP 失败隔离，停止自动重试")
        return None

    # ---- ② 取文件：stat 三态 / 下载 / 控损异常文件 --------------------

    def _fetch(self, remote_name: str, remote_path: str,
               local_path: Path) -> ImportResult | None:
        """None 表示本地文件已就绪，可以进入解析入库。"""
        # stat 三态：存在 / 尚未生成 / 其他错误
        try:
            present = self.source.exists(remote_path)
        except FileNotFoundError:
            present = False
        except Exception as e:
            self.log.error(f"stat 远程文件出错 {remote_name}: {e}")
            return ImportResult(remote_name, ImportStatus.FAILED,
                                detail=f"stat: {e}")
        if not present:
            self.log.info(f"远程文件尚未生成: {remote_name}")
            return ImportResult(remote_name, ImportStatus.MISSING)

        try:
            self.log.info(f"正在下载 {remote_name} ...")
            self.source.fetch(remote_path, local_path)
        except Exception as e:
            self.log.error(f"下载失败 {remote_name}: {e}")
            return ImportResult(remote_name, ImportStatus.FAILED,
                                detail=f"get: {e}")
        return self._reject_loss_control_file(remote_name, local_path)

    def _reject_loss_control_file(self, remote_name: str,
                                  local_path: Path) -> ImportResult | None:
        """控损：远端存在但拉下来是 0 字节，视为「文件有问题」。

        不写库、不登记清单、不推进水位，交给缺文件兜底处理，下轮继续重试。
        """
        if not self._is_loss_control():
            return None
        try:
            empty_file = local_path.stat().st_size == 0
        except OSError:
            empty_file = True
        if empty_file:
            self.log.error(
                f"控损 AVL 为空文件(0 字节)，按异常文件处理: {remote_name}")
            return ImportResult(remote_name, ImportStatus.FAILED,
                                detail="空文件(0 字节)")
        return None

    # ---- ③ 解析入库 ----------------------------------------------------

    def _import_local(self, remote_name: str, prefix: str,
                      local_path: Path) -> ImportResult:
        try:
            # 解析层是生成器；写入层按 BATCH_SIZE 分桶、逐批 commit
            rows = self.parser.parse(local_path)
            if self._drop_before_insert():
                self.log.info(self._write_mode_log(remote_name, prefix))
            written = self.sink.write(remote_name, rows, layout=self.layout)
            # 控损：文件非空但解析不出有效记录，同样视为「文件有问题」。
            # 此时不登记清单，下轮还能重试；水位也不会被空批次推进。
            rejected = self._reject_loss_control_batch(remote_name, written)
            if rejected is not None:
                return rejected
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
            return ImportResult(remote_name, ImportStatus.FAILED,
                                detail=f"import: {e}")

    def _reject_loss_control_batch(self, remote_name: str,
                                   written: int) -> ImportResult | None:
        """控损空批次：写库返回 0 行时按异常批次处理，下轮重试。"""
        if not self._is_loss_control() or written > 0:
            return None
        self.log.error(f"控损 AVL 无有效记录，按异常批次处理: {remote_name}")
        return ImportResult(remote_name, ImportStatus.FAILED,
                            detail="无有效记录(空批次)")

    # ---- 模式文案：由 sink 维护，runner 只决定何时打印 ------------------

    def _write_mode_log(self, remote_name: str, prefix: str) -> str:
        describe = getattr(self.sink, "write_mode_log", None)
        if callable(describe):
            return describe(remote_name, prefix)
        # 测试替身 / 其它 sink 实现：只保留与 profile 口径一致的通用描述。
        return (f"[{prefix}] 全量替换模式=1：{remote_name} 写入前 "
                "DROP 并重建全部目标表，只保留本批 AVL 数据；"
                "导入期间在线侧会读到空表（未启用双套表切换）")

    def _startup_mode_notice(self) -> str:
        describe = getattr(self.sink, "startup_notice", None)
        if callable(describe):
            return describe()
        return "导入期间表为空、在线侧静默降级，请安排在业务低峰并公告窗口"

    def _log_import_done(self, filename: str, prefix: str) -> None:
        """单文件入库完成计数。"""
        st = self.parser.stats
        self.log.info(
            f"[{prefix}] {filename} 覆盖完成，共 {st.rows} 条，"
            f"跳过空主键 {st.skipped} 条")

    # ---- 控损（F09591）缺文件兜底 --------------------------------------

    def _is_loss_control(self) -> bool:
        """是否控损任务；其余 AVL 完全不参与断供兜底。"""
        return getattr(self.profile, "project", None) == _LOSS_CONTROL_PROJECT

    def _expected_remote_name(self) -> str | None:
        """常驻轮次里「本应到达」的文件名：前一天日期的控损 AVL。

        例：26 日 02:00 轮询时，期望拿到 25 日的文件；上游 T+1 出文件，
        files 里没有它就是当天业务数据没到。
        """
        prefixes = getattr(self.profile, "file_prefixes", ())
        if not prefixes:
            return None
        day = (self._now() - timedelta(days=1)).strftime("%Y%m%d")
        return remote_name_for(self.profile, prefixes[0], day)

    def _roll_marker_path(self) -> Path | None:
        state_dir = getattr(self.profile, "state_dir", None)
        if state_dir is None:
            return None
        return Path(state_dir) / f"{self.profile.name}.op_time_roll"

    def _roll_already_done(self, day_key: str) -> bool:
        if self._last_roll_day == day_key:
            return True
        marker = self._roll_marker_path()
        if marker is None:
            return False
        try:
            return (marker.is_file()
                    and marker.read_text(encoding="utf-8").strip() == day_key)
        except Exception:
            return False

    def _mark_roll_done(self, day_key: str) -> None:
        self._last_roll_day = day_key
        marker = self._roll_marker_path()
        if marker is None:
            return
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(day_key, encoding="utf-8")
        except Exception as e:
            self.log.error(f"控损兜底标记写入失败 {marker}: {e}")

    def _maybe_roll_loss_control(self, results: list[ImportResult]) -> bool:
        """应到文件缺失 / 异常 / 空批次时，把库内数据 ``op_time`` 整体 +1 天。

        目的：上游断供时续住原有批次，既不被 ``cleanup_expired()`` 按真实
        日期清掉，也仍落在在线侧新鲜度窗口内，避免前台查不到数据。

        命中即停：本轮已有新数据成功入库、或应到文件此前已登记清单，都不推进。
        同一自然日最多推进一次（进程内闸门 + 标记文件），防止常驻轮询把
        日期反复往后平移。只作用于控损，且不改 ``_avl_meta`` 水位。
        """
        if not self._is_loss_control():
            return False
        day_key = self._now().strftime("%Y-%m-%d")
        if self._roll_already_done(day_key):
            return False

        # 本轮任意日期成功入库了有效数据 → 前台有数据，无需兜底
        if any(r.status == ImportStatus.OK and r.rows > 0 for r in results):
            return False

        expected = self._expected_remote_name()
        # 应到文件此前已成功入库（本轮 SKIP）→ 不是断供
        if expected is not None and any(
                r.filename == expected and r.status == ImportStatus.SKIP
                for r in results):
            return False

        roll = getattr(self.sink, "roll_forward_op_time", None)
        if not callable(roll):
            self.log.error(
                "控损缺文件兜底未执行：sink 不支持 roll_forward_op_time()")
            return False

        bad = [r for r in results
               if expected is None or r.filename == expected]
        reason = "; ".join(
            f"{r.filename}:{r.status}"
            + (f"({r.detail})" if r.detail else "")
            for r in bad) or "本轮无入库结果（如 SFTP 连接失败）"

        try:
            changed = roll(days=1)
        except Exception as e:
            self.log.error(f"控损缺文件兜底失败 op_time+1: {e}")
            return False

        self._mark_roll_done(day_key)
        self.log.info(
            f"[{self.profile.name}] 控损缺文件兜底：应到文件 {expected} "
            f"未取到有效数据 [{reason}]，库内 op_time 整体 +1 天，"
            f"更新 {changed} 行（_avl_meta 水位不动）")
        return True

    def run_once_for_date(self, date_str: str,
                          force_reload: bool = False,
                          results: list[ImportResult] | None = None) -> int:
        """返回本轮真正新入库的文件数；``results`` 收集每个文件的结构化结果。"""
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
                # P0：DROP 模式按文件累计连续失败，达到阈值即隔离停重试。
                self._track_drop_failure(result, force=force_reload)
                if results is not None:
                    results.append(result)
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
        # P0：全量替换模式每批写入前已 DROP 重建全部目标表，表内只保留
        # 最新一批、不存在需按 op_time 清理的历史行；跳过无索引全表扫描
        # （8000 万行/天下，7 天轮转每天约 18 张表 × 62.5 万行空扫）。
        # 人工 --date 单次补跑（force_full=True）保留原有全量清理语义。
        if self._drop_before_insert() and not force_full and should_purge:
            should_purge = False
            self.log.info(
                f"[{self.profile.name}] 全量替换模式跳过库表过期清理："
                "写入前已 DROP 重建全部目标表，表内无历史批次可清")
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
            if self._drop_before_insert():
                self.log.info(
                    f"[{self.profile.name}] 全量替换模式：轮询只负责检查远程文件"
                    "是否到达，已入库文件由 .fetched 清单跳过，不是导入调度周期；"
                    "单文件全量导入耗时可能远超轮询间隔。"
                    + self._startup_mode_notice())

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
                round_results: list[ImportResult] = []
                for d in sorted(set(dates)):
                    total += self.run_once_for_date(
                        d, force_reload=force_reload,
                        results=round_results)
                # 先续住 op_time，再跑过期清理：否则旧批次已被清掉就无从兜底。
                # 只在常驻轮询启用；--date 单次补跑是人工动作，不自动平移日期。
                self._maybe_roll_loss_control(round_results)
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
