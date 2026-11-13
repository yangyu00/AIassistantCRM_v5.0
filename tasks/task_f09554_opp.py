# -*- coding: utf-8 -*-
"""任务1：回捞商机（号卡 / 套餐 / 宽带）AVL 定时拉取与入库 —— 独立进程版。

对应原 Missions.md「任务1」，覆盖 F09554 / F09555 / F09556 三个前缀。
从 ``sftp_workdir_f09554/sftp_service.py`` 抽取，逻辑与原实现保持一致：
  * 远程文件：``{F09554|F09555|F09556}{YYYYMMDD}000000.AVL``
  * 制表符分隔，固定 12 列，一行一条记录，**无续行**
  * 一个前缀一个 sqlite 库（F09554.db / F09555.db / F09556.db），
    库内表名 ``data_{前缀}``，与在线查询 ``data_query.PRODUCT_TABLE_MAP`` 对齐
  * 列数不足补空串、超出截断；末列 city_id 强转 int，失败置 0
  * 追加模式入库（INSERT），按 op_time 过期清理

运行：
    常驻轮询：  python tasks/task_f09554_opp.py
    补跑某天：  python tasks/task_f09554_opp.py --date 20261110
    强制重导：  python tasks/task_f09554_opp.py --date 20261110 --force-reload
"""
import os
import gc
import sqlite3
import traceback

from .base_task import BaseSftpTask

# 复用原模块的 SFTP 连接配置，避免漂移（只拿配置，不改原代码）
from sftp_workdir_f09554.sftp_service import CONFIG as _SRC_CONFIG


class OppTask(BaseSftpTask):
    TASK_NAME = "f09554_opp"
    FILE_PREFIXES = list(_SRC_CONFIG["FILE_PREFIXES"])          # ["F09554","F09555","F09556"]
    SFTP = {
        "host": _SRC_CONFIG["SFTP_HOST"],
        "port": _SRC_CONFIG["SFTP_PORT"],
        "username": _SRC_CONFIG["SFTP_USERNAME"],
        "password": _SRC_CONFIG["SFTP_PASSWORD"],
        "remote_path": _SRC_CONFIG["SFTP_REMOTE_PATH"],
    }
    DATA_EXPIRE_DAYS = _SRC_CONFIG["DATA_EXPIRE_DAYS"]           # 30
    LOCAL_FILE_EXPIRE_DAYS = _SRC_CONFIG["LOCAL_FILE_EXPIRE_DAYS"]  # 7
    BATCH_SIZE = _SRC_CONFIG["BATCH_SIZE"]                       # 10000
    POLL_INTERVAL_SECONDS = 600                                  # 10 分钟一轮，可自行调

    # 与原实现完全一致的 12 列定义
    DB_SCHEMA = [
        ("op_time", "TEXT"),
        ("call_time", "TEXT"),
        ("product_no", "TEXT"),
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
        # 本任务一个前缀一个 .db，DB_PATH 指向库目录（基类仅用于建目录）
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
            cur.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({cols})")
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table_name}_product_op "
                f"ON {table_name} (product_no, op_time)"
            )
            conn.commit()
        finally:
            conn.close()

    def parse_and_save(self, avl_local_path: str):
        """流式解析单个 AVL 并入库到对应前缀的库（追加模式）。"""
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
                f"INSERT INTO {table_name} ({', '.join(self.DB_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in self.DB_COLUMNS)})"
            )
            batch = []
            row_count = 0

            # 与原实现一致：多编码兜底
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
                    # 末列 city_id 强转 int
                    try:
                        val = fields[-1].strip()
                        fields[-1] = int(val) if val else 0
                    except ValueError:
                        fields[-1] = 0
                    batch.append(tuple(fields))
                    row_count += 1
                    if len(batch) >= self.BATCH_SIZE:
                        cur.executemany(insert_sql, batch)
                        conn.commit()
                        batch = []
                        self.log.info(f"[{prefix}] 已追加 {row_count} 条...")
                if batch:
                    cur.executemany(insert_sql, batch)
                    conn.commit()
            self.log.info(f"[{prefix}] {filename} 追加完成，共 {row_count} 条")
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
