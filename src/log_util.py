# -*- encoding: utf-8 -*-
"""在线进程日志初始化。

配置值由宿主 ``main.py`` 通过 :func:`configure_logging` 传入。其余模块只取
``my_log``（loguru 全局 logger），是否在文件里落盘由宿主决定。
"""
from loguru import logger
from pathlib import Path
import time

my_log = logger


def configure_logging(log_dir: str = "") -> None:
    """按 log_dir 挂 INFO / ERROR 文件 sink；留空则写到项目根目录的 log/。

    ``log_dir`` 取值来自 ``main.py`` 顶部的模块级常量。
    """
    if log_dir:
        log_dir_path = Path(log_dir)
    else:
        log_dir_path = Path(__file__).resolve().parent.parent / 'log'

    log_dir_path.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(
        str(log_dir_path / f"INFO_{time.strftime('%Y-%m-%d')}.log"),
        level="INFO",
        filter=lambda record: record["level"].name == "INFO",
        rotation="00:00",  # 每天00:00 自动生成新的日志文件
        encoding="utf8",
        compression=None,  # 压缩归档旧日志文件
        retention="7 day",  # 保留最近7天的日志文件
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | (file:{file.name}:{line}) {message}",
        backtrace=True,  # 记录完整的异常堆栈跟踪信息
        diagnose=True  # 在日志中显示更多的诊断信息
    )

    logger.add(
        str(log_dir_path / f"ERROR_{time.strftime('%Y-%m-%d')}.log"),
        level="ERROR",
        filter=lambda record: record["level"].name == "ERROR",
        rotation="00:00",  # 每天00:00 自动生成新的日志文件
        encoding="utf8",
        compression=None,  # 压缩归档旧日志文件
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | (file:{file.name}:{line}) {message}",
        retention="7 day",  # 保留最近7天的日志文件
        backtrace=True,  # 记录完整的异常堆栈跟踪信息
        diagnose=True  # 在日志中显示更多的诊断信息
    )
