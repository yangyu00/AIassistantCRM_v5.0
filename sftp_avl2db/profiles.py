# -*- coding: utf-8 -*-
"""装配工厂：把各层组件拼成一个可直接运行的任务。

``build_task(profile)`` 是唯一的装配入口，其他项目若只想拿走某一层，
直接 import 对应模块即可，不必经过这里。全部依赖可替换：

    task = build_task(profile, local_dir="io_samples",   # 离线源
                      logger=InMemoryLogger())            # 不装 loguru
    rc = task.runner.serve_forever(only_date="20261110")

``build_all()`` 供 CLI 一次性加载仓库自带的两个任务（显式读 ``.env``）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import TaskProfileConfig, load_profiles
from .logging import TaskLogger, build_logger
from .parsers import build_parser
from .protocols import (
    RecordParser, RemoteSource, StateStore,
)
from .remote import build_remote_source
from .runner import Avl2DbRunner
from .sink import build_layout, build_sink
from .state import SingleInstanceLock, build_state


@dataclass
class AssembledTask:
    """一个装配完成、可立即运行的任务。

    字段全部公开，便于宿主项目替换任一组件后重新组装，也便于单测直接断言
    （例如读 ``task.state`` 看清单、读 ``task.sink.stats`` 看批次）。
    """

    profile: TaskProfileConfig
    log: TaskLogger
    state: StateStore
    lock: SingleInstanceLock
    source: RemoteSource
    parser: RecordParser
    layout: object
    sink: object
    runner: Avl2DbRunner

    def close(self) -> None:
        """关闭日志 sink（只移除自己注册的两个 id，不动宿主 logger）。"""
        closer = getattr(self.log, "close", None)
        if callable(closer):
            closer()


def build_task(profile: TaskProfileConfig, *, logger=None, local_dir=None,
               day=None, sleeper=None, now=None,
               batch_size=None, synchronous=None) -> AssembledTask:
    """按 ``profile`` 装配；所有可选参数都是注入点，缺省即生产默认。

    :param logger: 注入宿主 logger / ``InMemoryLogger``；``None`` 时
        ``TaskLogger`` 延迟导入 loguru 全局 logger
    :param local_dir: 给了就走 ``LocalDirRemoteSource``（离线，不连 SFTP、
        不需要 paramiko），否则走 ``SftpRemoteSource``
    :param day: 固定日志文件名里的日期，便于单测
    :param sleeper / now: 注入守护循环的 sleep 与时钟
    :param batch_size / synchronous: 透传给 ``SqliteSink``
    """
    log = build_logger(profile, logger=logger, day=day)
    state, lock = build_state(profile)
    source = build_remote_source(profile, local_dir=local_dir)
    parser = build_parser(profile)
    layout = build_layout(profile)
    sink = build_sink(profile, layout=layout,
                      batch_size=batch_size, synchronous=synchronous)
    runner = Avl2DbRunner(
        profile, source=source, parser=parser, sink=sink, layout=layout,
        state=state, lock=lock, log=log, sleeper=sleeper, now=now,
    )
    return AssembledTask(profile, log, state, lock, source, parser,
                         layout, sink, runner)


def build_all(env_path=None, project_root=None, **kwargs) -> dict[str, AssembledTask]:
    """加载仓库自带的两个任务并逐个装配（CLI 用）。"""
    profiles = load_profiles(env_path=env_path, project_root=project_root)
    return {name: build_task(profile, **kwargs)
            for name, profile in profiles.items()}


__all__ = ["AssembledTask", "build_task", "build_all"]
