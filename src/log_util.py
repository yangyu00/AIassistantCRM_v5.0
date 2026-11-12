from loguru import logger
from pathlib import Path
import time
from config import ImmutableYamlConfig

global_config = ImmutableYamlConfig()
if global_config['log_dir']:
    log_dir_path = Path(global_config['log_dir'])
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
    retention="7 day",  # 保留最近30天的日志文件
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
    retention="7 day",  # 保留最近30天的日志文件
    backtrace=True,  # 记录完整的异常堆栈跟踪信息
    diagnose=True  # 在日志中显示更多的诊断信息
)

my_log = logger
