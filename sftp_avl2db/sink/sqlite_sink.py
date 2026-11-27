"""SQLite 写入层：建表 / 流式分批写 / 过期清理 / 全量替换原子切换。

单库连接的 PRAGMA / 回滚 / 关闭统一由 ``_session()`` 管理；只有
``_write_rows()`` 因一轮写入要同时持有多库长连接而自行管理。

装配：``profiles.build_task()`` 对「分表 + 全量替换」的 AVL（当前即 F09633）
传 ``atomic_swap=True``；默认 False。

``atomic_swap`` 双套表原子切换，消除每日全量导入的在线侧空窗。

    当前套（在线侧读）  {AVL}_{i}          —— 表名派生规则不变，services/ 零改动
    暂存套（导入目标）  {AVL}_stage_{i}
    上一套（切换后）    {AVL}_old_{i}

流程：建空 stage 套 → 约 8 亿行（上游每日全量，2026-11 由 8000 万复核上调）
纯 INSERT 灌入 stage → 一次性建非唯一
``product_no`` 索引 → 单个事务里把 128 组
``{AVL}_{i}`` RENAME 成 ``{AVL}_old_{i}``、把 ``{AVL}_stage_{i}`` RENAME 成
``{AVL}_{i}``，并在同一事务推进 ``_avl_meta`` 水位 → COMMIT → 独立事务释放
old 套。RENAME 只改 schema 不搬数据，切换事务是毫秒级；WAL 下在线侧的
短读连接要么看到提交前的完整旧套、要么看到提交后的完整新套，任意时刻
``{AVL}_{i}`` 都存在且有数据，不存在「表被 DROP 但还没灌完」的空窗。

导入失败（解析 / 写 stage / 切换三者任一）时当前套完全未被触碰，在线侧
继续读上一日全量数据；不会出现「失败即空表」。
代价：磁盘峰值从约 8.4GB 升到约 16.8GB（当前 + stage 两套），需预留
≥25GB（含 WAL 峰值）。

临时吞吐开关（本次为 F09633 的「8 亿行 / 8 小时」窗口临时启用）：
    ``sftp_avl2db/.env`` 的可选键 ``F09633_SYNC_JOURNAL_OFF=1`` 经
    ``load_profiles()`` → ``build_task()`` → ``build_sink(unsafe_no_durability=True)``
    传到本层。开启后该 AVL 的**所有**写入连接改为
    ``PRAGMA journal_mode = OFF`` + ``PRAGMA synchronous = OFF``，即关闭
    回滚日志（WAL / rollback journal）与 fsync，换取最大吞吐。只作用于该
    AVL，其余 11 个 AVL 仍是 WAL + NORMAL；把键改回 ``0`` 并重启进程即恢复。
    代价：崩溃 / 断电可能损坏整个 ``F09633.db``，且 ``_atomic_switch()`` 的
    128 组 RENAME 事务不再具备回滚与 WAL 快照隔离，切换期间在线读可能撞上
    锁或半切换 schema。风险与恢复步骤见 ``sftp_avl2db/README.md`` 第 5 节。
"""
from __future__ import annotations

import copy
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from .schema import (
    AVL_META_TABLE, INSERT_MODES, TableSchema, create_meta_table_sql,
    upsert_meta_sql,
)

# 远程文件名 {AVL}{YYYYMMDD}000000.AVL：取末尾 000000 之前的 8 位日期。
_FILE_DATE_RE = re.compile(r"(\d{8})000000$")

# 全局未刷行数上限的默认值：分表数再多，内存里也只留这么多行。
# 10 万行是实测折中值：内存约 40~60MB，每批每张分表平均 780 行。
DEFAULT_GLOBAL_FLUSH_ROWS = 100000

# 双套表切换的表名后缀。当前套保持 ``{AVL}`` / ``{AVL}_{分表号}``，
# 在线侧 db_config.table_name() 的派生规则完全不用改。
STAGE_SUFFIX: str = "_stage"
OLD_SUFFIX: str = "_old"


def _configure(conn: sqlite3.Connection, *,
               unsafe_no_durability: bool = False) -> sqlite3.Cursor:
    """建表、写入共用的连接配置。

    默认（``False``）保持 ``WAL`` + ``synchronous=NORMAL``；仅当某个 AVL
    显式开启 ``<AVL>_SYNC_JOURNAL_OFF`` 时改为 ``journal_mode=OFF`` +
    ``synchronous=OFF``，完全关闭回滚日志与 fsync 换取吞吐。两种口径的
    PRAGMA 条数都是 2，``_session()`` 的连接计数契约不变。
    """
    cur = conn.cursor()
    if unsafe_no_durability:
        # 库仍是 WAL（常见于在线侧读连接正持有该库）时切换会静默失败，
        # PRAGMA 会返回实际模式而不是抛异常。必须显式拦住，避免「以为关了
        # 其实没关」，把 8 小时窗口白白耗在 fsync 与 WAL 写入上。
        row = cur.execute("PRAGMA journal_mode = OFF").fetchone()
        mode = str(row[0]).lower() if row else ""
        if mode != "off":
            raise RuntimeError(
                f"无法关闭回滚日志：PRAGMA journal_mode=OFF 实际返回 {mode!r}；"
                "请确认没有其它进程正在读写该 db（尤其是 WAL 下的在线读连接）")
        cur.execute("PRAGMA synchronous = OFF")
    else:
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA synchronous = NORMAL")
    return cur


@dataclass(frozen=True)
class DbHealth:
    """启动期库健康检查结果（只读探测，不改动库文件头）。"""

    status: str        # missing / healthy / corrupt / incomplete
    db_path: str
    reason: str = ""   # 人类可读的判定原因
    detail: str = ""   # 缺表清单 / quick_check 前几条错误等

    @property
    def is_bad(self) -> bool:
        """是否达到「删除损坏库并重建空库」的条件。"""
        return self.status in ("corrupt", "incomplete")


@dataclass
class WriteStats:
    """一次 ``write()`` 的可观测信号。"""

    rows: int = 0             # 交给 executemany 的逻辑记录数
    batches: int = 0          # executemany 调用次数
    commits: int = 0          # commit 次数
    connections: int = 0      # 打开的 sqlite 连接数
    purged: int = 0           # 最近一次 purge_expired 删除的行数
    dropped_tables: int = 0   # 全量替换模式下本次 DROP 并重建的业务表数
    max_op_time: str = ""     # 本文件写入记录里最大的 op_time（原样文本）
    rolled_rows: int = 0      # 最近一次 roll_forward_op_time() 更新的行数
    swapped_tables: int = 0   # P2：本次原子切换的表数（0 = 未启用 / 空批次）
    swap_seconds: float = 0.0  # P2：切换事务耗时（RENAME 只改 schema，毫秒级）
    stage_indexes: int = 0     # P2：本次在 stage 套上建立的查询索引数
    errors: list[tuple[str, str]] = field(default_factory=list)  # [(库路径, 异常文本)]


class SqliteSink:
    """满足 ``sftp_avl2db.protocols.RecordSink`` 的 SQLite 实现。"""

    def __init__(self, profile, layout, *, table_schema: TableSchema | None = None,
                 batch_size: int | None = None,
                 global_flush_rows: int | None = None,
                 insert_mode: str | None = None,
                 bulk_pragmas: bool = True,
                 atomic_swap: bool = False,
                 unsafe_no_durability: bool = False):
        self.profile = profile
        self.layout = layout
        self.schema = table_schema or TableSchema.from_profile(profile)
        # 用 ``is None`` 而不是 ``or``：显式传 0 / 空串必须能被校验拦住，
        # 不能被当成「未指定」悄悄回落到默认值。
        self.batch_size = int(profile.retention.batch_size
                              if batch_size is None else batch_size)
        if self.batch_size <= 0:
            raise ValueError(f"batch_size 必须为正整数，当前为 {self.batch_size}")
        self._full_replace = self.drop_before_insert_enabled()
        # 一次写入用哪种 INSERT，只在这里决定；SQL 文本全部由
        # ``TableSchema.insert_sql()`` 生成，调用方不再参与拼串。
        # ``insert_mode`` 仅作装配层注入点，None = 按「表能力 + 是否全量替换」推导。
        if insert_mode is not None and insert_mode not in INSERT_MODES:
            raise ValueError(
                f"insert_mode 只能是 {'/'.join(INSERT_MODES)}，"
                f"当前为 {insert_mode!r}")
        if not self.schema.physical_pk:
            # 无唯一约束的表没有冲突可覆盖，REPLACE / upsert 都不成立。
            self.insert_mode = "plain"
        elif insert_mode is not None:
            self.insert_mode = insert_mode
        else:
            self.insert_mode = "replace" if self._full_replace else "upsert"
        self.bulk_pragmas = bool(bulk_pragmas)
        # 仅该 sink 所属 AVL 开启时为真；决定 _connect() / _session() 里
        # _configure() 走 WAL+NORMAL 还是 OFF+OFF。
        self.unsafe_no_durability = bool(unsafe_no_durability)
        # 只对分表 AVL 启用全局水位；固定表保持原有「单桶满批提交」语义。
        num_tables = int(getattr(layout, "num_tables", 0) or 0)
        if global_flush_rows is None:
            self.global_flush_rows = (
                DEFAULT_GLOBAL_FLUSH_ROWS if num_tables > 1 else 0)
        else:
            self.global_flush_rows = int(global_flush_rows)
        if self.global_flush_rows < 0:
            raise ValueError("global_flush_rows 不能为负")

        # ---- P2：双套表原子切换 ------------------------------------------
        self.atomic_swap = bool(atomic_swap)
        self._stage_layout = None
        if self.atomic_swap:
            if not self._full_replace:
                raise ValueError(
                    "atomic_swap 只在全量替换（drop_before_insert=1）模式下有意义")
            if not all(hasattr(layout, a) for a in ("base", "db_path", "num_tables")):
                raise ValueError(
                    "atomic_swap 需要 AvlDbLayout 提供 base / db_path / num_tables")
            # 浅拷贝布局，只换表名基准：db_path 保持完全一致，保证 RENAME
            # 发生在同一个 db 文件内（跨文件无法用 ALTER TABLE 原子切换）。
            stage = copy.copy(layout)
            stage.base = f"{layout.base}{STAGE_SUFFIX}"
            self._stage_layout = stage

        self._sql_cache: dict[str, str] = {}
        self.stats = WriteStats()

    # ---- 表名与布局 ------------------------------------------------------

    def _table_names(self):
        """三套表名：当前 / 暂存 / 上一套，顺序严格对齐。"""
        current = [t for _, t in self.layout.all_targets()]
        stage = [t for _, t in self._stage_layout.all_targets()]
        from ..avl_specs import table_name as make_table_name
        base = f"{self.layout.base}{OLD_SUFFIX}"
        n = int(self.layout.num_tables or 0)
        old = ([make_table_name(base, i) for i in range(n)] if n
               else [make_table_name(base)])
        if not (len(current) == len(stage) == len(old)):
            raise RuntimeError("三套表名数量不一致，拒绝切换")
        return current, stage, old

    def _group_targets_by_db(self, layout=None) -> dict[Path, list[str]]:
        """把布局枚举出的 ``(库, 表)`` 按库文件分组。"""
        layout = self.layout if layout is None else layout
        groups: dict[Path, list[str]] = {}
        for db_path, table in layout.all_targets():
            groups.setdefault(Path(db_path), []).append(table)
        return groups

    def drop_before_insert_enabled(self) -> bool:
        """全量替换模式是否开启；profile 没带该字段时按关闭处理。"""
        retention = getattr(self.profile, "retention", None)
        return int(getattr(retention, "drop_before_insert", 0) or 0) == 1

    def drop_existing(self, layout=None) -> int:
        """DROP 本 AVL 的全部业务表，再按同一 DDL 立即重建（清掉历史数据）。

        ``_avl_meta`` 是批次水位表，不进 layout，保留不动；表名来自静态布局，
        不接受外部字符串。返回被重建的业务表数量。
        """
        layout = self.layout if layout is None else layout
        dropped = 0
        for db_path, tables in self._group_targets_by_db(layout).items():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._session(db_path) as (conn, cur):
                for t in tables:
                    cur.execute(f"DROP TABLE IF EXISTS {t}")
                    cur.execute(self.schema.create_table_sql(t))
                    dropped += 1
                # 元数据表与业务表同库，幂等保留；不因业务表 DROP 丢失水位。
                cur.execute(create_meta_table_sql())
                conn.commit()
        return dropped

    def ensure_schema(self) -> None:
        """按布局枚举全部库表并建表。"""
        for db_path, tables in self._group_targets_by_db().items():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._session(db_path) as (conn, cur):
                for t in tables:
                    cur.execute(self.schema.create_table_sql(t))
                # 元数据表与业务表同库，但不进 layout，不参与分表 / 过期清理。
                cur.execute(create_meta_table_sql())
                conn.commit()

    # ---- 启动期库健康自检（OFF+OFF 崩溃兜底） ----------------------------

    def startup_health_check(self, *, full: bool = False) -> DbHealth:
        """进程启动、拿锁之后检查本 AVL 的 db 是否可安全继续写。

        默认只做**快速检查**（毫秒级，不扫表）：文件头、能否打开、128 张
        当前套表 + ``_avl_meta`` 是否齐全、每张表 B-tree 根页是否可读。这能
        抓到「崩溃截断 / 页写坏 / 原子切换到一半留下半套 schema」，而这正是
        ``journal_mode=OFF`` 下最常见的两种损坏形态。

        ``full=True`` 追加 ``PRAGMA quick_check`` 全表页校验，能抓到中间页
        损坏，但 8 亿行会全库扫描、耗时小时级，会吃掉导入窗口，故默认关闭。

        检查用**裸连接**、不套 ``_configure()``：避免检查动作本身把库头从
        WAL 改成非 WAL（或反过来）。下面全是只读语句。
        """
        db_path = Path(self.layout.db_path)
        expected = [t for _, t in self.layout.all_targets()]
        if not db_path.is_file():
            return DbHealth("missing", str(db_path),
                            "库文件不存在（首轮启动，按正常流程建库）")
        try:
            with db_path.open("rb") as f:
                head = f.read(16)
        except OSError as e:
            return DbHealth("corrupt", str(db_path), f"库文件无法读取: {e}")
        if head != b"SQLite format 3\x00":
            return DbHealth(
                "corrupt", str(db_path),
                f"库文件头非法（前 16 字节 {head!r}），疑似截断或写坏")

        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error as e:
            return DbHealth("corrupt", str(db_path), f"无法打开库: {e}")
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            absent = [t for t in expected if t not in names]
            if AVL_META_TABLE not in names:
                absent.append(AVL_META_TABLE)
            if absent:
                return DbHealth(
                    "incomplete", str(db_path),
                    f"库内表结构不完整（缺 {len(absent)} 张表），"
                    "疑似原子切换事务中途崩溃",
                    "缺表样例: " + ", ".join(absent[:6]))
            # 逐表触碰 B-tree 根页：LIMIT 1 不扫表，128 张表合计毫秒级。
            for t in expected:
                conn.execute(f"SELECT 1 FROM {t} LIMIT 1")
            if full:
                msgs = [str(r[0])
                        for r in conn.execute("PRAGMA quick_check").fetchall()]
                if msgs != ["ok"]:
                    return DbHealth("corrupt", str(db_path),
                                    "PRAGMA quick_check 未通过",
                                    "; ".join(msgs[:5]))
        except sqlite3.DatabaseError as e:
            return DbHealth("corrupt", str(db_path),
                            f"只读探测触发 SQLite 错误: {e}")
        finally:
            conn.close()
        return DbHealth(
            "healthy", str(db_path),
            f"结构完整、{len(expected)} 张表根页可读"
            + ("，quick_check 通过" if full else "（未跑全量页校验）"))

    def rebuild_corrupt_db(self, health: DbHealth) -> str:
        """直接删除损坏库并重建空库，不保留任何副本，返回处置说明。

        损坏库没有保留价值：OFF+OFF 下写坏的数据本就不完整、也无法信任；
        8 亿行量级的副本还会持续占住磁盘，把重建 / 本轮灌入的空间挤没。
        因此连主库带 ``-wal`` / ``-shm`` / ``-journal`` 残留一起 ``unlink``，
        不做改名、不留 ``*.corrupt.*``，立刻把空间释放出来。

        重建后由 runner 清空 ``.fetched`` 清单与失败隔离标记，强制本轮重灌。
        需要事后取证请在启动前自行做文件级备份，本方法不代劳。
        """
        db_path = Path(health.db_path)
        siblings = [db_path] + [Path(str(db_path) + s)
                                for s in ("-wal", "-shm", "-journal")]
        present = [p for p in siblings if p.is_file()]
        total = sum(p.stat().st_size for p in present)
        for p in present:
            p.unlink()
        action = (f"已直接删除 {len(present)} 个文件（共 {total} 字节），"
                  "未保留任何副本，磁盘空间已立即释放")
        # 空库重建：128 张当前套表 + _avl_meta，在线侧读空按既有降级不 500。
        self.ensure_schema()
        return action

    def _connect(self, db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), timeout=30,
                               cached_statements=256 if self.bulk_pragmas else 128)
        self.stats.connections += 1
        cur = _configure(conn,
                         unsafe_no_durability=self.unsafe_no_durability)
        if self.bulk_pragmas:
            # 大事务下默认 2MB page cache 会频繁把脏页刷回磁盘；
            # 64MB 缓存 + 内存临时表对千万级导入是纯收益，重启进程即失效。
            cur.execute("PRAGMA cache_size = -65536")
            cur.execute("PRAGMA temp_store = MEMORY")
            cur.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def _session(self, db_path: Path, *, bulk: bool = False
                 ) -> Iterator[tuple[sqlite3.Connection, sqlite3.Cursor]]:
        """单库连接的唯一生命周期：PRAGMA → 业务动作 → 回滚/关闭。

        ``bulk=True`` 走 ``_connect()``（额外 cache_size / temp_store /
        busy_timeout，并累计 ``stats.connections``）；``False`` 不计数，
        与建表 / 清理路径一致。两者都把 ``self.unsafe_no_durability`` 传给
        ``_configure()``：默认 WAL + synchronous=NORMAL，该 AVL 开启临时
        吞吐开关时改为 journal_mode=OFF + synchronous=OFF。

        事务边界仍归调用方：``commit()`` 在 with 内显式执行，本上下文不替
        调用方决定何时提交。异常统一 rollback 后原样抛出；「记 errors 后继续」
        还是「直接失败」由调用方是否包 try/except 决定，不塞进布尔参数。

        ``_write_rows()`` 是唯一例外：一轮写入要同时持有多库长连接，自行管理。
        """
        if bulk:
            conn = self._connect(db_path)
            cur = conn.cursor()
        else:
            conn = sqlite3.connect(str(db_path))
            cur = _configure(conn,
                         unsafe_no_durability=self.unsafe_no_durability)
        try:
            yield conn, cur
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _sql_for(self, table: str) -> str:
        """缓存 ``insert_sql()`` 的结果，避免每批重新拼串 / prepare。"""
        sql = self._sql_cache.get(table)
        if sql is None:
            sql = self._sql_cache[table] = self.schema.insert_sql(
                table, self.insert_mode)
        return sql

    def _flush(self, conn: sqlite3.Connection, table: str, batch: list) -> None:
        if not batch:
            return
        conn.executemany(self._sql_for(table), batch)
        conn.commit()
        self.stats.batches += 1
        self.stats.commits += 1

    def _time_index(self) -> int:
        """``op_time`` 列下标；表结构没有时间列时返回 -1（不收集水位）。"""
        try:
            return self.schema.column_names.index(self.schema.time_column)
        except ValueError:
            return -1

    @staticmethod
    def _file_date(filename: str) -> str:
        """从 ``{AVL}{YYYYMMDD}000000.AVL`` 取文件名日期；无法识别返回空串。"""
        m = _FILE_DATE_RE.search(Path(filename).stem)
        return m.group(1) if m else ""

    def _record_metadata(self, filename: str, layout, *, conn=None) -> None:
        """入库成功后推进 ``_avl_meta`` 水位。

        ``conn`` 为空（默认，非切换路径）：独立连接写入，元数据写失败只记
        ``stats.errors``，不回滚业务数据、不阻断 runner 登记清单。

        ``conn`` 传入（切换路径）：在**切换事务内**执行，不 commit / 不 close，
        异常向上抛由切换事务整体回滚，保证「数据 + 水位」原子一致。
        """
        file_date = self._file_date(filename)
        targets = list(layout.all_targets())
        if not targets:
            return
        db_path = Path(targets[0][0])
        params = (
            self.profile.name,
            file_date,
            self.stats.max_op_time,
            Path(filename).name,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        if conn is not None:
            conn.execute(create_meta_table_sql())
            if file_date:
                conn.execute(upsert_meta_sql(), params)
            return
        if not file_date:
            return
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._session(db_path) as (own, _):
                # 幂等：即使调用方没先跑 ensure_schema() 也能登记。
                own.execute(create_meta_table_sql())
                own.execute(upsert_meta_sql(), params)
                own.commit()
        except Exception as e:
            # 元数据写失败只记 errors，不回滚业务数据、不阻断 runner 登记清单。
            self.stats.errors.append((str(db_path), f"meta: {e}"))

    def _make_router(self, layout, filename: str):
        """逐行路由：分表 AVL 走预生成表名的查表，其它布局回退原接口。"""
        num_tables = int(getattr(layout, "num_tables", 0) or 0)
        if (num_tables > 1 and hasattr(layout, "product_no_index")
                and hasattr(layout, "base") and hasattr(layout, "db_path")):
            from ..avl_specs import shard_index, table_name
            pidx = int(layout.product_no_index)
            db_key = str(Path(layout.db_path))
            names = [table_name(layout.base, i) for i in range(num_tables)]

            def route(row):
                return db_key, names[shard_index(row[pidx], num_tables)]
            return route

        def route(row):
            db_path, table = layout.target(filename, row)
            return str(Path(db_path)), table
        return route

    # ---- 灌入主体（原 P1 write() 拆出，行为不变） ------------------------

    def _write_rows(self, filename: str, rows: Iterable[tuple], layout,
                    *, drop_targets: bool = True) -> int:
        """把 rows 按 layout 分桶、逐桶/全局满批提交；不登记水位。

        ``drop_targets=True`` 时先 DROP 重建目标表（原全量替换语义）；
        双套表切换路径传 False，因为 stage 套已由 ``_prepare_stage()`` 建好。
        """
        time_index = self._time_index()
        col_count = len(self.schema.columns)

        if drop_targets and self.drop_before_insert_enabled():
            self.stats.dropped_tables = self.drop_existing(layout)

        route = self._make_router(layout, filename)
        conns: dict[str, sqlite3.Connection] = {}
        batches: dict[tuple[str, str], list] = {}
        pending = 0

        def ensure_conn(db_key: str) -> sqlite3.Connection:
            conn = conns.get(db_key)
            if conn is None:
                Path(db_key).parent.mkdir(parents=True, exist_ok=True)
                conn = self._connect(Path(db_key))
                conns[db_key] = conn
            return conn

        def flush_bucket(key: tuple[str, str]) -> None:
            """原语义：单个分桶攒满 BATCH_SIZE，立即提交一次。"""
            nonlocal pending
            batch = batches.get(key)
            if not batch:
                return
            flushed = len(batch)
            self._flush(conns[key[0]], key[1], batch)
            batch.clear()
            pending -= flushed

        def flush_all() -> None:
            """新语义：全局累计达到阈值，同库所有桶一次事务提交。"""
            nonlocal pending
            by_db: dict[str, list[tuple[str, list]]] = {}
            for (db_key, table), batch in batches.items():
                if batch:
                    by_db.setdefault(db_key, []).append((table, batch))
            for db_key, items in by_db.items():
                conn = conns[db_key]
                for table, batch in items:
                    conn.executemany(self._sql_for(table), batch)
                    batch.clear()
                    self.stats.batches += 1
                conn.commit()
                self.stats.commits += 1
            pending = 0

        try:
            for row in rows:
                if type(row) is not tuple:
                    row = tuple(row)
                if len(row) != col_count:
                    raise ValueError(
                        f"记录列数 {len(row)} 与表结构 {col_count} 列不一致")
                if time_index >= 0:
                    op_time = str(row[time_index])
                    if op_time > self.stats.max_op_time:
                        self.stats.max_op_time = op_time
                db_key, table = route(row)
                ensure_conn(db_key)
                key = (db_key, table)
                bucket = batches.get(key)
                if bucket is None:
                    bucket = batches[key] = []
                bucket.append(row)
                self.stats.rows += 1
                pending += 1
                if len(bucket) >= self.batch_size:
                    flush_bucket(key)
                elif self.global_flush_rows and pending >= self.global_flush_rows:
                    flush_all()

            # 末尾余量：按库一次提交
            flush_all()
        except Exception:
            for conn in conns.values():
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            for conn in conns.values():
                conn.close()
            conns.clear()
            batches.clear()
        return self.stats.rows

    # ---- P2：双套表原子切换 ----------------------------------------------

    def _prepare_stage(self) -> int:
        """清掉上一轮遗留的 old 套，重建空的 stage 套；当前套完全不动。

        先 DROP old（约 8.4GB）再建 stage，保证导入期间磁盘峰值始终是
        「当前 + stage」两套，不会叠加第三套。返回重建的 stage 表数。
        """
        _, _, old = self._table_names()
        count = 0
        for db_path, stage_tables in self._group_targets_by_db(
                self._stage_layout).items():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._session(db_path, bulk=True) as (conn, cur):
                for t in old:
                    cur.execute(f"DROP TABLE IF EXISTS {t}")
                for t in stage_tables:
                    cur.execute(f"DROP TABLE IF EXISTS {t}")
                    cur.execute(self.schema.create_table_sql(t))
                    count += 1
                cur.execute(create_meta_table_sql())
                conn.commit()
                self.stats.commits += 1
        return count

    def _atomic_switch(self, filename: str) -> None:
        """单事务把「当前套→old、stage→当前」，并在同事务推进水位。

        RENAME 只改 schema 不搬数据；128 组 RENAME 全在一个事务里，WAL 下
        在线侧短读连接只会看到提交前或提交后的完整一套，任意时刻
        ``{AVL}_{i}`` 都存在且有数据。
        """
        current, stage, old = self._table_names()
        db_path = Path(self.layout.db_path)
        t0 = time.perf_counter()
        with self._session(db_path, bulk=True) as (conn, cur):
            cur.execute("BEGIN IMMEDIATE")
            for cur_t, old_t, stg_t in zip(current, old, stage):
                # 首轮导入兜底：当前表可能尚未建（ensure_schema 未跑）。
                # CREATE IF NOT EXISTS 对已有数据的当前表是 no-op。
                cur.execute(self.schema.create_table_sql(cur_t))
                cur.execute(f"ALTER TABLE {cur_t} RENAME TO {old_t}")
                cur.execute(f"ALTER TABLE {stg_t} RENAME TO {cur_t}")
            # 水位与业务数据在同一个事务里提交，避免切换后水位落后一批。
            self._record_metadata(filename, self.layout, conn=conn)
            conn.commit()
            self.stats.commits += 1
        self.stats.swapped_tables = len(current)
        self.stats.swap_seconds = round(time.perf_counter() - t0, 3)

    def _cleanup_old(self) -> None:
        """切换提交后释放 old 套（独立事务）。失败只记 errors，不影响正确性：
        下一轮 ``_prepare_stage()`` 会先 DROP 它们。"""
        _, _, old = self._table_names()
        db_path = Path(self.layout.db_path)
        try:
            with self._session(db_path, bulk=True) as (conn, cur):
                for t in old:
                    cur.execute(f"DROP TABLE IF EXISTS {t}")
                conn.commit()
                self.stats.commits += 1
        except Exception as e:
            # 失败只记 errors，不影响已切换的数据；下一轮 _prepare_stage() 兜底。
            self.stats.errors.append((str(db_path), f"cleanup_old: {e}"))

    def _build_stage_indexes(self) -> int:
        """stage 套灌完后一次性建查询索引。

        上游全量 AVL 保证 ``(product_no, scene)`` 唯一，写入侧不做任何去重：
        去掉物理主键与 REPLACE 后不再有冲突可收敛，也不跑全表扫描；若有重复
        行会原样入库；在线侧 ``services/diagnosis/query.py`` 对 ``pain_points``
        按首次出现去重兜底，不会输出重复文案。索引名带本轮 token：ALTER TABLE
        RENAME 不改索引名，固定名会在下一轮重建 stage 套时撞上仍挂在 old 套
        上的同名索引。
        """
        token = uuid.uuid4().hex[:8]
        built = 0
        for db_path, tables in self._group_targets_by_db(
                self._stage_layout).items():
            with self._session(db_path, bulk=True) as (conn, _):
                for t in tables:
                    conn.execute(
                        f"CREATE INDEX idx_{t}_pn_{token} ON {t}(product_no)")
                    built += 1
                conn.commit()
                self.stats.commits += 1
        self.stats.stage_indexes = built
        return built

    def _write_atomic_swap(self, filename: str, rows: Iterable[tuple],
                           layout) -> int:
        """双套表全量导入编排：建 stage → 灌 stage → 建索引 → 原子切换 → 释放 old。"""
        # a) 当前套完全不动，在线侧此时读到的仍是上一日全量数据
        self.stats.dropped_tables = self._prepare_stage()
        # b) 灌进 stage（drop_targets=False，表已建好）
        self._write_rows(filename, rows, self._stage_layout,
                         drop_targets=False)
        # c) 空批次不切换也不推进水位，保留当前套
        if self.stats.rows <= 0:
            return 0
        self._build_stage_indexes()
        self._atomic_switch(filename)
        # d) 释放 old 套；失败不影响已切换的数据
        self._cleanup_old()
        return self.stats.rows

    # ---- 模式说明文案：由写入层维护，编排层只决定何时打印 ----------------

    def startup_notice(self) -> str:
        """守护进程启动时的写入模式说明（追加在 runner 自己的轮询说明之后）。"""
        if self.atomic_swap:
            return ("已启用双套表原子切换：导入期间在线侧持续读上一日全量数据，"
                    "无空窗；切换事务为秒级，磁盘需预留两套数据空间")
        return "导入期间表为空、在线侧静默降级，请安排在业务低峰并公告窗口"

    def write_mode_log(self, remote_name: str, prefix: str) -> str:
        """单文件导入前的全量替换模式说明；文案随本 sink 的实现走。"""
        if self.atomic_swap:
            return (f"[{prefix}] 全量替换模式=1（双套表原子切换）："
                    f"{remote_name} 先灌入 staging 套，全部完成后单事务 "
                    "RENAME 切换，在线侧全程读当前套、无空窗")
        return (f"[{prefix}] 全量替换模式=1：{remote_name} 写入前 "
                "DROP 并重建全部目标表，只保留本批 AVL 数据；"
                "导入期间在线侧会读到空表（未启用双套表切换）")

    def write(self, filename: str, rows: Iterable[tuple], layout=None) -> int:
        """把解析器产出的记录按布局分桶、逐桶/全局满批提交，并登记批次水位。"""
        layout = self.layout if layout is None else layout
        self.stats = WriteStats()
        self._sql_cache.clear()

        if self.atomic_swap:
            return self._write_atomic_swap(filename, rows, layout)

        self._write_rows(filename, rows, layout, drop_targets=True)
        # 业务数据已全部提交且没有异常：才推进水位。空批次不登记水位，
        # 避免把 latest_op_time 写成空串让在线侧新鲜度窗口失去基准。
        if self.stats.rows > 0:
            self._record_metadata(filename, layout)
        return self.stats.rows

    def purge_expired(self, cutoff: str,
                      tables: Iterable[str] | None = None) -> int:
        """按 ``op_time < cutoff`` 清理库表，返回删除总行数。"""
        total = 0
        self.stats.purged = 0
        self.stats.errors = []

        groups = self._group_targets_by_db()
        if tables is not None:
            picked = set(tables)
            groups = {db: [t for t in ts if t in picked]
                      for db, ts in groups.items()}
            # 本轮没有命中任何表时连 DB 都不打开
            groups = {db: ts for db, ts in groups.items() if ts}

        for db_path, targets in groups.items():
            if not db_path.is_file():
                continue
            try:
                with self._session(db_path) as (conn, cur):
                    for t in targets:
                        cur.execute(self.schema.delete_expired_sql(t), (cutoff,))
                        total += max(cur.rowcount, 0)
                    conn.commit()
            except Exception as e:
                # 单库失败记 errors 后继续清理其余库，不中断整轮。
                self.stats.errors.append((str(db_path), str(e)))

        self.stats.purged = total
        return total

    def roll_forward_op_time(self, days: int = 1,
                             tables: Iterable[str] | None = None) -> int:
        """控损断供兜底：把业务表里的 ``op_time`` 日期整体后移 ``days`` 天。"""
        days = int(days)
        if days <= 0:
            raise ValueError(f"days 必须为正整数，当前为 {days}")
        total = 0
        self.stats.rolled_rows = 0
        self.stats.errors = []

        groups = self._group_targets_by_db()
        if tables is not None:
            picked = set(tables)
            groups = {db: [t for t in ts if t in picked]
                      for db, ts in groups.items()}
            groups = {db: ts for db, ts in groups.items() if ts}

        for db_path, targets in groups.items():
            if not db_path.is_file():
                continue
            try:
                with self._session(db_path, bulk=True) as (conn, cur):
                    for t in targets:
                        cur.execute(self.schema.shift_time_sql(t, days))
                        total += max(cur.rowcount, 0)
                    conn.commit()
            except Exception as e:
                # 单库失败记 errors 后继续处理其余库，不中断断供兜底。
                self.stats.errors.append((str(db_path), str(e)))

        self.stats.rolled_rows = total
        return total


def build_sink(profile, *, layout=None, table_schema: TableSchema | None = None,
               batch_size: int | None = None,
               global_flush_rows: int | None = None,
               insert_mode: str | None = None,
               bulk_pragmas: bool = True,
               atomic_swap: bool = False,
               unsafe_no_durability: bool = False) -> SqliteSink:
    """装配写入层。``atomic_swap`` 默认关闭，``insert_mode=None`` 自动推导；
    ``unsafe_no_durability`` 默认关闭（WAL + NORMAL），由 ``build_task()``
    按 profile 的 per-AVL 开关注入。"""
    if layout is None:
        from .router import build_layout
        layout = build_layout(profile)
    return SqliteSink(profile, layout, table_schema=table_schema,
                      batch_size=batch_size,
                      global_flush_rows=global_flush_rows,
                      insert_mode=insert_mode,
                      bulk_pragmas=bulk_pragmas,
                      atomic_swap=atomic_swap,
                      unsafe_no_durability=unsafe_no_durability)


__all__ = ["SqliteSink", "WriteStats", "DbHealth", "build_sink",
           "STAGE_SUFFIX", "OLD_SUFFIX"]
