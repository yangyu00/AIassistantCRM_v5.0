# -*- coding: utf-8 -*-
"""任务2：控损 AVL 定时拉取与入库。

远程文件 F09591{YYYYMMDD}000000.AVL，制表符分隔，逻辑 5 列：
op_time | user_id | product_no | city_id | strategy_name。

strategy_name（第 5 列）内部含裸换行，一条逻辑记录横跨多个物理行：
以行首 YYYY-MM-DD 判定新记录起始，其余物理行作为续行用 \\n 拼回第 5 列。
按 product_no 分 128 张表，以 product_no 为主键覆盖入库。

运行（项目根目录）：
    python -m tasks.task_f09591_loss
    python -m tasks.task_f09591_loss --date 20261110
    python -m tasks.task_f09591_loss --date 20261110 --force-reload
"""
import os
import re
import gc
import zlib
import sqlite3
import traceback
from typing import List

from .base_task import BaseSftpTask
from .config import TASKS

_CONF = TASKS["f09591_loss"]


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _strip_eol(line: str) -> str:
    content = line.rstrip("\n")
    # 只剥一个 ASCII 反斜杠：rstrip('\\') 会误删 GBK 尾字节为 0x5C 的汉字。
    if content.endswith("\\"):
        content = content[:-1]
    return content


class LossTask(BaseSftpTask):
    TASK_NAME = "f09591_loss"
    FILE_PREFIXES = _CONF["FILE_PREFIXES"]
    SFTP = _CONF["SFTP"]
    DATA_EXPIRE_DAYS = _CONF["DATA_EXPIRE_DAYS"]
    LOCAL_FILE_EXPIRE_DAYS = _CONF["LOCAL_FILE_EXPIRE_DAYS"]
    BATCH_SIZE = _CONF["BATCH_SIZE"]
    NUM_TABLES = _CONF["NUM_TABLES"]
    TABLE_PREFIX = _CONF["TABLE_NAME_PREFIX"]
    POLL_INTERVAL_SECONDS = _CONF["POLL_INTERVAL_SECONDS"]

    DB_SCHEMA = [
        ("op_time", "TEXT"),
        ("user_id", "TEXT"),
        ("product_no", "TEXT NOT NULL"),
        ("city_id", "TEXT"),
        ("strategy_name", "TEXT"),
    ]
    DB_COLUMNS = [c[0] for c in DB_SCHEMA]

    def __init__(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.LOCAL_DIR = os.path.join(base_dir, "tasks", "files", self.TASK_NAME)
        self.DB_DIR = os.path.join(base_dir, "tasks", "sqlite_db", self.TASK_NAME)
        self.DB_PATH = os.path.join(self.DB_DIR, f"{self.FILE_PREFIXES[0]}.db")
        self._tables = [f"{self.TABLE_PREFIX}_{i}" for i in range(self.NUM_TABLES)]
        super().__init__()

    def init_db(self):
        os.makedirs(os.path.dirname(self.DB_PATH), exist_ok=True)
        conn = sqlite3.connect(self.DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode = WAL")
            cols = ", ".join(f"{c} {t}" for c, t in self.DB_SCHEMA)
            for t in self._tables:
                cur.execute(f"CREATE TABLE IF NOT EXISTS {t} ({cols}, PRIMARY KEY (product_no))")
            conn.commit()
        finally:
            conn.close()

    def parse_and_save(self, avl_local_path: str):
        conn = None
        try:
            os.makedirs(os.path.dirname(self.DB_PATH), exist_ok=True)
            conn = sqlite3.connect(self.DB_PATH)
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode = WAL")
            cur.execute("PRAGMA synchronous = NORMAL")
            cur.execute("PRAGMA cache_size = -64000")
            cur.execute("PRAGMA temp_store = MEMORY")

            batches: dict = {t: [] for t in self._tables}
            total = 0
            skipped = 0
            buffer = None

            with open(avl_local_path, "r", encoding="utf-8", errors="ignore") as f:
                self.log.info("开始流式解析（按日期行重组续行）...")
                for line in f:
                    content = _strip_eol(line)
                    if not content:
                        continue
                    if DATE_RE.match(content):
                        if buffer is not None:
                            if str(buffer[2]).strip():
                                self._route(buffer, batches)
                                total += 1
                                self._flush_full(cur, conn, batches)
                            else:
                                skipped += 1
                        buffer = self._split_head(content)
                        if buffer is None:
                            self.log.warning(f"跳过格式严重错误行: {content[:30]}...")
                    elif buffer is not None:
                        buffer[4] += "\n" + content

                if buffer is not None:
                    if str(buffer[2]).strip():
                        self._route(buffer, batches)
                        total += 1
                    else:
                        skipped += 1

            self._flush_remaining(cur, conn, batches)
            self.log.info(
                f"{os.path.basename(avl_local_path)} 解析完成，共 {total} 条，"
                f"跳过空手机号 {skipped} 条"
            )
        except Exception as e:
            self.log.error(f"处理失败: {e}\n{traceback.format_exc()}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
            gc.collect()

    def _split_head(self, content: str) -> List[str] or None:
        """把日期起始行切成 5 列；第 5 列允许为空。不可用时返回 None。"""
        parts = content.split("\t")
        if len(parts) >= 5:
            return [parts[0], parts[1], parts[2], parts[3], parts[4]]
        if len(parts) == 4:
            return [parts[0], parts[1], parts[2], parts[3], ""]
        return None

    def _route(self, row: List[str], batches: dict):
        """按 product_no 的 crc32 取模路由到 128 张表之一。"""
        p_no = row[2] if row[2] else "NULL"
        idx = zlib.crc32(p_no.encode("utf-8")) % self.NUM_TABLES
        batches[self._tables[idx]].append(tuple(row))

    def _flush_full(self, cur, conn, batches: dict):
        for t, batch in batches.items():
            if len(batch) >= self.BATCH_SIZE:
                cur.executemany(
                    f"INSERT OR REPLACE INTO {t} ({', '.join(self.DB_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in self.DB_COLUMNS)})",
                    batch,
                )
                conn.commit()
                self.log.info(f"表 {t} 写入 {len(batch)} 条")
                batch.clear()

    def _flush_remaining(self, cur, conn, batches: dict):
        for t, batch in batches.items():
            if batch:
                cur.executemany(
                    f"INSERT OR REPLACE INTO {t} ({', '.join(self.DB_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in self.DB_COLUMNS)})",
                    batch,
                )
                conn.commit()
                self.log.info(f"表 {t} 最终提交 {len(batch)} 条")
                batch.clear()

    def _cleanup_db(self):
        from datetime import datetime, timedelta
        expire = (datetime.now() - timedelta(days=self.DATA_EXPIRE_DAYS)).strftime("%Y-%m-%d")
        if not os.path.exists(self.DB_PATH):
            return
        conn = None
        try:
            conn = sqlite3.connect(self.DB_PATH)
            cur = conn.cursor()
            for t in self._tables:
                cur.execute(f"DELETE FROM {t} WHERE op_time < ?", (expire,))
                if cur.rowcount > 0:
                    self.log.info(f"表 {t} 清理过期 {cur.rowcount} 条")
            conn.commit()
        except Exception as e:
            self.log.error(f"清理过期数据失败: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()


if __name__ == "__main__":
    LossTask.main()
