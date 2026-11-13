# -*- coding: utf-8 -*-
"""任务1：回捞商机（号卡 / 套餐 / 宽带）AVL 定时拉取与入库。

远程文件 {F09554|F09555|F09556}{YYYYMMDD}000000.AVL，制表符分隔 12 列，
一行一条记录，无续行。每个前缀一个 .db，按 product_no 主键覆盖入库。

运行（项目根目录）：
    python -m tasks.task_f09554_opp
    python -m tasks.task_f09554_opp --date 20261110
    python -m tasks.task_f09554_opp --date 20261110 --force-reload
"""
import os
import gc
import sqlite3
import traceback

from .base_task import BaseSftpTask
from .config import TASKS

_CONF = TASKS["f09554_opp"]


class OppTask(BaseSftpTask):
    TASK_NAME = "f09554_opp"
    FILE_PREFIXES = _CONF["FILE_PREFIXES"]
    SFTP = _CONF["SFTP"]
    DATA_EXPIRE_DAYS = _CONF["DATA_EXPIRE_DAYS"]
    LOCAL_FILE_EXPIRE_DAYS = _CONF["LOCAL_FILE_EXPIRE_DAYS"]
    BATCH_SIZE = _CONF["BATCH_SIZE"]
    POLL_INTERVAL_SECONDS = _CONF["POLL_INTERVAL_SECONDS"]

    DB_SCHEMA = [
        ("op_time", "TEXT"),
        ("call_time", "TEXT"),
        ("product_no", "TEXT NOT NULL"),
        ("type_dl", "VARCHAR(50)"),
        ("type_xl", "VARCHAR(50)"),
        ("content_abstract", "TEXT"),
        ("cust_demand", "TEXT"),
        ("rec_strategy", "TEXT"),
        ("reject_reason", "TEXT"),
        ("reason_note", "TEXT"),
        ("refer_strategy", "TEXT"),
        ("city_id", "INTEGER"),
    ]
    DB_COLUMNS = [c[0] for c in DB_SCHEMA]

    def __init__(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.LOCAL_DIR = os.path.join(base_dir, "tasks", "files", self.TASK_NAME)
        self.DB_DIR = os.path.join(base_dir, "tasks", "sqlite_db", self.TASK_NAME)
        self.DB_PATH = self.DB_DIR
        self._db_path_of = {p: os.path.join(self.DB_DIR, f"{p}.db") for p in self.FILE_PREFIXES}
        super().__init__()

    def init_db(self):
        for prefix, db_path in self._db_path_of.items():
            self._ensure_table(db_path, f"data_{prefix}")

    def _ensure_table(self, db_path, table_name):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode = WAL")
            cols = ", ".join(f"{c} {t}" for c, t in self.DB_SCHEMA)
            cur.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({cols}, PRIMARY KEY (product_no))")
            conn.commit()
        finally:
            conn.close()

    def parse_and_save(self, avl_local_path: str):
        """流式解析 AVL，按 product_no 覆盖入库。"""
        filename = os.path.basename(avl_local_path)
        prefix = next((p for p in self.FILE_PREFIXES if filename.startswith(p)), None)
        if prefix is None:
            self.log.error(f"无法从文件名识别前缀，跳过入库: {filename}")
            return
        db_path = self._db_path_of[prefix]
        table_name = f"data_{prefix}"

        conn = None
        try:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode = WAL")
            cur.execute("PRAGMA synchronous = OFF")
            cur.execute("PRAGMA cache_size = -64000")
            cur.execute("PRAGMA temp_store = MEMORY")

            insert_sql = (
                f"INSERT OR REPLACE INTO {table_name} ({', '.join(self.DB_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in self.DB_COLUMNS)})"
            )
            batch = []
            row_count = 0
            skipped = 0

            fh = None
            for enc in ("utf-8", "gbk", "latin-1"):
                try:
                    fh = open(avl_local_path, "r", encoding=enc, errors="ignore")
                    self.log.info(f"使用编码 {enc} 读取 {filename}")
                    break
                except Exception:
                    continue
            if fh is None:
                raise RuntimeError("无法识别文件编码")

            with fh as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    fields = line.split("\t")
                    if len(fields) < len(self.DB_COLUMNS):
                        fields.extend([""] * (len(self.DB_COLUMNS) - len(fields)))
                    elif len(fields) > len(self.DB_COLUMNS):
                        fields = fields[:len(self.DB_COLUMNS)]
                    try:
                        val = fields[-1].strip()
                        fields[-1] = int(val) if val else 0
                    except ValueError:
                        fields[-1] = 0
                    if not str(fields[2]).strip():
                        skipped += 1
                        continue
                    batch.append(tuple(fields))
                    row_count += 1
                    if len(batch) >= self.BATCH_SIZE:
                        cur.executemany(insert_sql, batch)
                        conn.commit()
                        batch = []
                        self.log.info(f"[{prefix}] 已覆盖入库 {row_count} 条...")
                if batch:
                    cur.executemany(insert_sql, batch)
                    conn.commit()
            self.log.info(
                f"[{prefix}] {filename} 覆盖完成，共 {row_count} 条，"
                f"跳过空手机号 {skipped} 条"
            )
        except Exception as e:
            self.log.error(f"[{prefix}] 处理 {filename} 失败: {e}\n{traceback.format_exc()}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
            gc.collect()

    def _cleanup_db(self):
        from datetime import datetime, timedelta
        expire = (datetime.now() - timedelta(days=self.DATA_EXPIRE_DAYS)).strftime("%Y-%m-%d")
        for prefix, db_path in self._db_path_of.items():
            if not os.path.exists(db_path):
                continue
            conn = None
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(f"DELETE FROM data_{prefix} WHERE op_time < ?", (expire,))
                conn.commit()
                if cur.rowcount > 0:
                    self.log.info(f"[{prefix}] 清理过期 {cur.rowcount} 条 (op_time < {expire})")
            except Exception as e:
                self.log.error(f"[{prefix}] 清理过期数据失败: {e}")
                if conn:
                    conn.rollback()
            finally:
                if conn:
                    conn.close()


if __name__ == "__main__":
    OppTask.main()
