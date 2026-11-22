# -*- coding: utf-8 -*-
"""任务日志：为每个任务挂 INFO / ERROR 两个文件 sink，且不动宿主 logger。"""
from __future__ import annotations

import time
from pathlib import Path

LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | ({file.name}:{line}) {message}"


class _BoundLogger:
    """``InMemoryLogger.bind()`` 的返回对象：只负责转发并带上 extra。"""

    def __init__(self, parent, extra):
        self._parent = parent
        self._extra = extra

    def info(self, message, *args, **kwargs):
        self._parent._emit("INFO", message, self._extra)

    def error(self, message, *args, **kwargs):
        self._parent._emit("ERROR", message, self._extra)

    def warning(self, message, *args, **kwargs):
        self._parent._emit("WARNING", message, self._extra)


class InMemoryLogger:
    """零依赖的日志替身（满足 ``TaskLogger`` 的注入接口）。"""

    def __init__(self):
        self.records = []
        self.sinks = {}
        self.remove_calls = []
        self._next_id = 0

    def bind(self, **extra):
        return _BoundLogger(self, dict(extra))

    def add(self, sink, *args, **kwargs):
        self._next_id += 1
        self.sinks[self._next_id] = {"sink": sink, "kwargs": dict(kwargs)}
        return self._next_id

    def remove(self, sink_id=None):
        self.remove_calls.append(sink_id)
        if sink_id is None:
            self.sinks.clear()
        else:
            self.sinks.pop(sink_id, None)

    def _emit(self, level, message, extra):
        self.records.append({"level": level, "message": str(message),
                             "extra": dict(extra)})

    def info(self, message, *args, **kwargs):
        self._emit("INFO", message, {})

    def error(self, message, *args, **kwargs):
        self._emit("ERROR", message, {})

    def warning(self, message, *args, **kwargs):
        self._emit("WARNING", message, {})


class TaskLogger:
    """单个任务一套 INFO / ERROR 文件日志。"""

    def __init__(self, task_name, log_dir, *, logger=None, day=None):
        self.task_name = str(task_name)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._day = day or time.strftime("%Y-%m-%d")
        self._sink_ids = []

        if logger is None:
            try:
                from loguru import logger as loguru_logger
            except ImportError as exc:
                raise RuntimeError(
                    "TaskLogger 默认使用 loguru；请安装 loguru，"
                    "或注入 logger=InMemoryLogger() / 宿主 logger 对象"
                ) from exc
            logger = loguru_logger

        self._logger = logger
        self.log = logger.bind(task=self.task_name)
        self._add_sinks()

    @property
    def info_log_path(self) -> Path:
        return self.log_dir / f"{self.task_name}_INFO_{self._day}.log"

    @property
    def error_log_path(self) -> Path:
        return self.log_dir / f"{self.task_name}_ERROR_{self._day}.log"

    def _filter(self, level_name):
        name = self.task_name

        def _f(record):
            return (record["level"].name == level_name
                    and record["extra"].get("task") == name)
        return _f

    def _add_sinks(self):
        # 只 add 自己的 sink；绝不调用 remove() 清空全局配置。
        kwargs = dict(rotation="00:00", retention="7 day", encoding="utf8",
                      format=LOG_FORMAT)
        self._sink_ids.append(self._logger.add(
            str(self.info_log_path), level="INFO",
            filter=self._filter("INFO"), **kwargs))
        self._sink_ids.append(self._logger.add(
            str(self.error_log_path), level="ERROR",
            filter=self._filter("ERROR"),
            backtrace=True, diagnose=True, **kwargs))

    def info(self, message, *args, **kwargs):
        return self.log.info(message, *args, **kwargs)

    def error(self, message, *args, **kwargs):
        return self.log.error(message, *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        return self.log.warning(message, *args, **kwargs)

    def close(self):
        """只移除自己注册的两个 sink，绝不动宿主的其他 sink。"""
        for sid in self._sink_ids:
            try:
                self._logger.remove(sid)
            except Exception:
                pass
        self._sink_ids.clear()


def build_logger(profile, *, logger=None, day=None) -> TaskLogger:
    """按 ``TaskProfileConfig`` 装配日志。"""
    return TaskLogger(profile.name, profile.log_dir, logger=logger, day=day)


__all__ = ["TaskLogger", "InMemoryLogger", "LOG_FORMAT", "build_logger"]
