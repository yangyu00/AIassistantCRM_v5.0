import subprocess
import sys
from pathlib import Path

from src.log_util import my_log

SFTP_SERVICE_PATH = Path(__file__).resolve().parent / 'sftp_service.py'


def run_sftp_service_task():
    """
    使用子进程同步执行 sftp_service.py 脚本。
    此函数将在一个独立的后台线程中运行，不会阻塞 FastAPI 的主事件循环。
    """
    my_log.info("SFTP_FO9554 数据更新任务：正在启动子进程...")
    try:
        # 获取当前 Python 解释器的路径和 sftp_service.py 的绝对路径
        python_executable = sys.executable

        # 使用 subprocess.run 同步执行子进程
        # 这会阻塞当前的后台线程，直到子进程执行完毕，但不会阻塞 FastAPI 主进程
        result = subprocess.run([python_executable, SFTP_SERVICE_PATH])

        if result.returncode == 0:
            my_log.info("SFTP_FO9554 数据更新任务：子进程执行成功。")
        else:
            my_log.error(f"SFTP_FO9554 数据更新任务：子进程执行失败，返回码={result.returncode}。")

    except Exception as e:
        my_log.error(f"SFTP_FO9554 数据更新任务：启动子进程时发生异常 - {e}")
