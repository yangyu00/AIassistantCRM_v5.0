"""装配工厂：把各层组件拼成可直接运行的任务。"""
from __future__ import annotations

from dataclasses import dataclass

from .config import TaskProfileConfig, load_profiles
from .protocols import RecordParser, RecordSink, RemoteSource, StateStore, TableLayout
from .runner import Avl2DbRunner
from .sink import build_layout, build_sink
from .sink.schema import TableSchema
from .parsers import build_parser
from .remote import build_remote_source
from .state import SingleInstanceLock, build_state
from .task_logging import TaskLogger, build_logger


@dataclass
class AssembledTask:
    """一个装配完成、可立即运行的任务。"""

    profile: TaskProfileConfig
    log: TaskLogger
    state: StateStore
    lock: SingleInstanceLock
    source: RemoteSource
    parser: RecordParser
    layout: TableLayout
    sink: RecordSink
    runner: Avl2DbRunner

    def close(self) -> None:
        """只关闭本任务注册的文件日志 sink，不动宿主 logger。"""
        closer = getattr(self.log, "close", None)
        if callable(closer):
            closer()


def build_task(profile: TaskProfileConfig, *, logger=None, local_dir=None,
               day=None, sleeper=None, now=None, batch_size=None,
               table_schema: TableSchema | None = None,
               atomic_swap: bool | None = None) -> AssembledTask:
    """按 ``profile`` 装配。

    ``atomic_swap`` 为 None（默认）时自动判断：分表 + 全量替换的 AVL 启用
    双套表原子切换（当前恰好只命中 F09633），消除每日全量导入期间在线侧
    读到空表的窗口。显式传 True/False 可强制开启 / 回退到原 DROP 语义。

    写入模式（``plain`` / ``replace`` / ``upsert``）由 sink 按「表能力 +
    是否全量替换」自行推导，装配层不再计算也不传参。

    在线侧 ``services/`` 不需要任何改动：当前套表名仍是 ``{AVL}_{分表号}``，
    切换是同一 db 文件内的 RENAME。
    """
    log = build_logger(profile, logger=logger, day=day)
    state, lock = build_state(profile)
    source = build_remote_source(profile, local_dir=local_dir)
    parser = build_parser(profile)
    layout = build_layout(profile)
    if atomic_swap is None:
        atomic_swap = bool(
            getattr(profile, "sharded", False)
            and int(getattr(profile.retention, "drop_before_insert", 0) or 0) == 1)
    sink = build_sink(profile, layout=layout, table_schema=table_schema,
                      batch_size=batch_size, atomic_swap=atomic_swap)
    runner = Avl2DbRunner(
        profile, source=source, parser=parser, sink=sink, layout=layout,
        state=state, lock=lock, log=log, sleeper=sleeper, now=now,
    )
    return AssembledTask(profile, log, state, lock, source, parser,
                         layout, sink, runner)


def build_all(runtime_dir=None, **kwargs) -> dict[str, AssembledTask]:
    """加载包内 ``.env`` 声明的全部 AVL 并逐个装配（CLI 用）。"""
    profiles = load_profiles(runtime_dir=runtime_dir)
    return {name: build_task(profile, **kwargs)
            for name, profile in profiles.items()}


__all__ = ["AssembledTask", "build_task", "build_all"]
