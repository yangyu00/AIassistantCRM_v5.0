# -*- coding: utf-8 -*-
"""定时任务公共基类。

把原 ``main.py`` startup 里托管的 3 个 SFTP 拉取任务抽成**独立进程**后，
三个任务高度同构（连 SFTP、找当天 AVL、下载、解析入库、清理、休眠），
故把公共能力收敛到这里，具体任务只需覆写「库表结构 / 解析入库 / 配置」三处。

设计要点（单机单实例，刻意不做分布式/高可用）：
1. 断点续传：每个任务在本地维护一个 ``.fetched`` 清单文件，记录「已成功入库」的
   远程文件名。每轮轮询先跳过清单里的文件，入库成功后追加，避免重复拉取与重复入库。
2. 跨进程互斥：与仍可能存在的 ``main.py`` startup 线程 / 其它副本之间，用
   ``.lock`` 文件 + ``portalocker``（缺失时退化为 ``fcntl``）做单机排他锁，
   保证同一时刻只有一个拉取者在跑，不会把同一个 AVL 下载入库两次。
3. 独立日志：每个任务一套自己的 INFO/ERROR 日志，落到 ``tasks/log/``，
   与在线业务日志分开，便于单独排障与调优。
4. 全量只认「当天 + 前一天」：远程按 ``{前缀}{YYYYMMDD}000000.AVL`` 命名，
   每天只会有当天一个文件；补跑昨天用 ``--date`` 显式指定。

本模块不修改任何原始代码，仅被 tasks/ 下的新脚本引用。
"""
import os
import re
import sys
import json
import time
import fcntl
import socket
import argparse
import traceback
from datetime import datetime, timedelta

import paramiko

try:
    import portalocker  # 可选依赖，有则优先用
except Exception:  # pragma: no cover
    portalocker = None


# 远程文件名：{前缀}{YYYYMMDD}000000.AVL
REMOTE_NAME_TMPL = "{prefix}{date}000000.AVL"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class SingleInstanceLock:
    """单机单实例文件锁。拿不到锁立即失败（非阻塞），避免两个拉取者并发出库。"""

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fh = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        self._fh = open(self.lock_path, "w", encoding="utf-8")
        try:
            if portalocker is not None:
                portalocker.lock(self._fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self._fh.close()
            self._fh = None
            return False
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(json.dumps({
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False))
        self._fh.flush()
        return True

    def release(self):
        if self._fh is None:
            return
        try:
            if portalocker is not None:
                portalocker.unlock(self._fh)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


class FetchedManifest:
    """已成功入库的远程文件名清单（断点续传 / 不重复拉取）。

    以「追加写 + 行级 flush」方式落盘，进程被 kill 也不会丢失已完成记录；
    每行一个文件名，启动与每轮轮询时读入内存 set。
    """

    def __init__(self, manifest_path: str):
        self.manifest_path = manifest_path
        self._done = set()
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                for line in f:
                    name = line.strip()
                    if name:
                        self._done.add(name)

    def has(self, remote_filename: str) -> bool:
        return remote_filename in self._done

    def add(self, remote_filename: str):
        if remote_filename in self._done:
            return
        with open(self.manifest_path, "a", encoding="utf-8") as f:
            f.write(remote_filename + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._done.add(remote_filename)


class BaseSftpTask:
    """单个 SFTP 拉取任务的守护进程骨架。子类需实现 ``parse_and_save``。"""

    # ---- 子类必须覆盖 ----
    TASK_NAME = "base"          # 任务名，用于日志/锁/清单文件命名
    FILE_PREFIXES = []          # 该任务负责的远程文件前缀列表，如 ["F09554","F09555"]
    SFTP = {}                   # {host,port,username,password,remote_path}
    LOCAL_DIR = ""              # 本地 AVL 落盘目录
    DB_PATH = ""                # sqlite 库路径
    DATA_EXPIRE_DAYS = 30       # 库表数据保留天数
    LOCAL_FILE_EXPIRE_DAYS = 7  # 本地 AVL 保留天数
    POLL_INTERVAL_SECONDS = 600 # 轮询间隔（秒）
    BATCH_SIZE = 5000

    def __init__(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        log_dir = os.path.join(base_dir, "tasks", "log")
        os.makedirs(log_dir, exist_ok=True)
        self._setup_logger(log_dir)
        self.manifest = FetchedManifest(
            os.path.join(base_dir, "tasks", "state", f"{self.TASK_NAME}.fetched")
        )
        self.lock = SingleInstanceLock(
            os.path.join(base_dir, "tasks", "state", f"{self.TASK_NAME}.lock")
        )

    # ---- 日志：每个任务一套独立 INFO/ERROR 文件 ----
    def _setup_logger(self, log_dir):
        from loguru import logger
        self.log = logger.bind(task=self.TASK_NAME)
        logger.remove()
        day = time.strftime("%Y-%m-%d")
        logger.add(
            os.path.join(log_dir, f"{self.TASK_NAME}_INFO_{day}.log"),
            level="INFO",
            filter=lambda r: r["level"].name == "INFO" and r["extra"].get("task") == self.TASK_NAME,
            rotation="00:00", retention="7 day", encoding="utf8",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | ({file.name}:{line}) {message}",
        )
        logger.add(
            os.path.join(log_dir, f"{self.TASK_NAME}_ERROR_{day}.log"),
            level="ERROR",
            filter=lambda r: r["level"].name == "ERROR" and r["extra"].get("task") == self.TASK_NAME,
            rotation="00:00", retention="7 day", encoding="utf8", backtrace=True, diagnose=True,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | ({file.name}:{line}) {message}",
        )

    # ---- 子类实现：把已下载到本地的 AVL 解析入库 ----
    def parse_and_save(self, avl_local_path: str):
        raise NotImplementedError

    # ---- 可选覆盖：建表 / 过期清理 ----
    def init_db(self):
        pass

    def cleanup_expired(self):
        self._cleanup_local_files()
        self._cleanup_db()

    def _cleanup_local_files(self):
        expire = datetime.now() - timedelta(days=self.LOCAL_FILE_EXPIRE_DAYS)
        for name in os.listdir(self.LOCAL_DIR):
            if not name.endswith(".AVL"):
                continue
            fp = os.path.join(self.LOCAL_DIR, name)
            if datetime.fromtimestamp(os.path.getmtime(fp)) < expire:
                try:
                    os.remove(fp)
                    self.log.info(f"已删除过期本地文件: {name}")
                except Exception as e:
                    self.log.error(f"删除过期本地文件失败 {name}: {e}")

    def _cleanup_db(self):
        raise NotImplementedError

    # ---- SFTP 连接 ----
    def _connect(self):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            self.SFTP["host"], int(self.SFTP["port"]),
            self.SFTP["username"], self.SFTP["password"],
        )
        return ssh, ssh.open_sftp()

    def _remote_name(self, prefix: str, date_str: str) -> str:
        return REMOTE_NAME_TMPL.format(prefix=prefix, date=date_str)

    # ---- 单轮：对单个日期跑一次「拉取+入库」 ----
    def run_once_for_date(self, date_str: str, force_reload: bool = False) -> int:
        """返回本轮真正新入库的文件数。"""
        imported = 0
        os.makedirs(self.LOCAL_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(self.DB_PATH), exist_ok=True)
        self.init_db()  # 幂等：首次运行自动建表，避免无表导致入库失败
        ssh = sftp = None
        try:
            ssh, sftp = self._connect()
            self.log.info("SFTP 连接成功")
        except Exception as e:
            self.log.error(f"SFTP 连接失败: {e}")
            return 0

        try:
            for prefix in self.FILE_PREFIXES:
                remote_name = self._remote_name(prefix, date_str)
                remote_path = f"{self.SFTP['remote_path'].rstrip('/')}/{remote_name}"
                local_path = os.path.join(self.LOCAL_DIR, remote_name)

                if self.manifest.has(remote_name) and not force_reload:
                    self.log.info(f"SKIP 已在清单中，跳过: {remote_name}")
                    continue

                try:
                    sftp.stat(remote_path)
                except FileNotFoundError:
                    self.log.info(f"远程文件尚未生成: {remote_name}")
                    continue
                except Exception as e:
                    self.log.error(f"stat 远程文件出错 {remote_name}: {e}")
                    continue

                try:
                    self.log.info(f"正在下载 {remote_name} ...")
                    sftp.get(remote_path, local_path)
                except Exception as e:
                    self.log.error(f"下载失败 {remote_name}: {e}")
                    continue

                try:
                    self.parse_and_save(local_path)
                    self.manifest.add(remote_name)   # 入库成功后才记清单
                    imported += 1
                    self.log.info(f"入库完成并登记清单: {remote_name}")
                except Exception as e:
                    self.log.error(f"解析入库失败 {remote_name}: {e}\n{traceback.format_exc()}")
                    # 入库失败不登记清单，下轮会重试；本地文件保留
        finally:
            try:
                if sftp:
                    sftp.close()
                if ssh:
                    ssh.close()
            except Exception:
                pass
        return imported

    # ---- 守护循环 ----
    def serve_forever(self, only_date: str = None, force_reload: bool = False):
        if not self.lock.acquire():
            self.log.error(f"获取任务锁失败，已有 {self.TASK_NAME} 实例在运行，退出。")
            return 1
        try:
            os.makedirs(self.LOCAL_DIR, exist_ok=True)
            os.makedirs(os.path.dirname(self.DB_PATH), exist_ok=True)
            self.init_db()
            self.log.info(
                f"{self.TASK_NAME} 守护进程启动 pid={os.getpid()} "
                f"轮询间隔={self.POLL_INTERVAL_SECONDS}s 前缀={self.FILE_PREFIXES}"
            )

            # 单次模式：拉完指定日期就退出（配合 crontab 也可）
            if only_date:
                n = self.run_once_for_date(only_date, force_reload=force_reload)
                self.cleanup_expired()
                self.log.info(f"单次模式结束，本次新入库 {n} 个文件")
                return 0

            while True:
                today = datetime.now().strftime("%Y%m%d")
                # 每轮同时兜底前一天，防止跨天瞬间漏拉
                dates = [(datetime.now() - timedelta(days=1)).strftime("%Y%m%d"), today]
                total = 0
                for d in dict.fromkeys(dates):
                    total += self.run_once_for_date(d, force_reload=force_reload)
                self.cleanup_expired()
                self.log.info(f"本轮结束，新入库 {total} 个文件，休眠 {self.POLL_INTERVAL_SECONDS}s")
                time.sleep(self.POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            self.log.info("收到中断信号，退出。")
            return 0
        except Exception as e:
            self.log.error(f"守护循环异常退出: {e}\n{traceback.format_exc()}")
            return 1
        finally:
            self.lock.release()

    @classmethod
    def main(cls, argv=None):
        parser = argparse.ArgumentParser(description=f"{cls.TASK_NAME} 定时拉取与入库守护进程")
        parser.add_argument("--date", type=str, default=None,
                            help="单次模式：只拉取指定日期(YYYYMMDD)后退出；不传则常驻轮询")
        parser.add_argument("--force-reload", action="store_true",
                            help="本地已存在/已在清单中的文件也强制重新下载并入库")
        args = parser.parse_args(argv)
        if args.date:
            try:
                datetime.strptime(args.date, "%Y%m%d")
            except ValueError:
                print("ERROR:: 日期格式错误，请使用 YYYYMMDD", flush=True)
                sys.exit(2)
        sys.exit(cls().serve_forever(only_date=args.date, force_reload=args.force_reload))
