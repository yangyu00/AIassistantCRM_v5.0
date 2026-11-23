"""在线进程日志初始化。"""
from loguru import logger
from pathlib import Path
import time

my_log = logger


def configure_logging(log_dir: str = "") -> None:
    """按 log_dir 挂 INFO / ERROR 文件 sink。"""
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
        rotation="00:00",  # 每天 00:00 轮转
        encoding="utf8",
        compression=None,  # 不压缩归档
        retention="7 day",  # 保留最近 7 天
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | (file:{file.name}:{line}) {message}",
        backtrace=True,  # 记录完整异常堆栈
        diagnose=True  # 显示更多诊断信息
    )

    logger.add(
        str(log_dir_path / f"ERROR_{time.strftime('%Y-%m-%d')}.log"),
        level="ERROR",
        filter=lambda record: record["level"].name == "ERROR",
        rotation="00:00",
        encoding="utf8",
        compression=None,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | (file:{file.name}:{line}) {message}",
        retention="7 day",
        backtrace=True,
        diagnose=True
    )
