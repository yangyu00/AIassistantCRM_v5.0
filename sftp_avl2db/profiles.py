# -*- coding: utf-8 -*-
"""装配工厂：把各层组件拼成可直接运行的任务。

``build_task(profile)`` 是唯一装配入口；分层与调用链见 ``Readme.md``。
"""
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
    """一个装配完成、可立即运行的任务。

    字段全部公开且带契约类型，便于宿主项目替换任一组件后重新组装，也便于
    单测直接断言（例如读 ``task.state`` 看清单、读 ``task.sink.stats`` 看批次）。
    """

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
        """只关闭本任务注册的文件日志 sink，不动宿主 logger。

        ``source`` / ``lock`` 由 ``Avl2DbRunner`` 的轮次与进程生命周期管理，
        这里不重复关闭。
        """
        closer = getattr(self.log, "close", None)
        if callable(closer):
            closer()


def build_task(profile: TaskProfileConfig, *, logger=None, local_dir=None,
               day=None, sleeper=None, now=None, batch_size=None,
               synchronous=None, table_schema: TableSchema | None = None) -> AssembledTask:
    """按 ``profile`` 装配；所有可选参数都是显式注入点，缺省即生产默认。

    :param logger: 注入宿主 logger / ``InMemoryLogger``；``None`` 时
        ``TaskLogger`` 延迟导入 loguru 全局 logger
    :param local_dir: 给了就走 ``LocalDirRemoteSource``（离线，不连 SFTP、
        不需要 paramiko），否则走 ``SftpRemoteSource``
    :param day: 固定日志文件名里的日期，便于单测
    :param sleeper / now: 注入守护循环的 sleep 与时钟
    :param batch_size / synchronous / table_schema: 透传给 ``SqliteSink``
    """
    log = build_logger(profile, logger=logger, day=day)
    state, lock = build_state(profile)
    source = build_remote_source(profile, local_dir=local_dir)
    parser = build_parser(profile)
    layout = build_layout(profile)
    sink = build_sink(profile, layout=layout, table_schema=table_schema,
                      batch_size=batch_size, synchronous=synchronous)
    runner = Avl2DbRunner(
        profile, source=source, parser=parser, sink=sink, layout=layout,
        state=state, lock=lock, log=log, sleeper=sleeper, now=now,
    )
    return AssembledTask(profile, log, state, lock, source, parser,
                         layout, sink, runner)


def build_all(runtime_dir=None, **kwargs) -> dict[str, AssembledTask]:
    """加载包内 ``.env`` 声明的全部 AVL 并逐个装配（CLI 用）。

    ``runtime_dir`` 只改产出位置，不改配置来源；不传即包内 ``runtime/``。
    其余关键字参数透传给 ``build_task()`` 的注入点。
    """
    profiles = load_profiles(runtime_dir=runtime_dir)
    return {name: build_task(profile, **kwargs)
            for name, profile in profiles.items()}


__all__ = ["AssembledTask", "build_task", "build_all"]
