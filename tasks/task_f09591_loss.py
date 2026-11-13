# -*- coding: utf-8 -*-
"""任务2：控损 AVL 定时拉取与入库 —— 独立进程版。

对应原 Missions.md「任务2」，前缀 F09591，**单文件约 7000 万条**，故做了分表 + 续行拼接。
从 ``sftp_workdir_f09591/sftp_service.py`` 抽取，规则与原实现完全一致：

【文件结构】
  远程文件 ``F09591{YYYYMMDD}000000.AVL``，UTF-8，制表符分隔，**逻辑上 5 列**：
      op_time | user_id | product_no | city_id | strategy_name
  但 ``strategy_name``（第 5 列，控损策略正文）内部含**裸换行符**，一个逻辑记录会
  横跨多个物理行，因此不能按行 split，必须按「行首是否日期」重组逻辑记录。

【续行拼接规则（关键）】
  1. 逐物理行读；以 ``YYYY-MM-DD`` 开头的物理行 = 一条新逻辑记录的起始行；
  2. 不以日期开头的物理行 = 上一条记录的「续行」，属于第 5 列 strategy_name 的内容；
  3. 续行拼回第 5 列时，用 ``\\n`` 还原被拆开的换行（即多物理行 join 后再整体切列）；
  4. 每个物理行行尾可能带一个反斜杠 ``\\``（源文件续行标记），读取时 rstrip 掉；
  5. 起始行按 tab 切：>=5 列 → 前 4 列取元数据、第 5 列起拼成 strategy_name；
     ==4 列 → 视作第 5 列为空；<4 列 → 判定为损坏行，跳过。
  6. 文件末尾残留的最后一条逻辑记录要单独 flush。

【分表规则】
  128 张表 ``data_F09591_0 .. data_F09591_127``，落在同一个 ``F09591.db`` 文件中，
  路由键 = ``zlib.crc32(product_no) % 128``（product_no 为空时用字面量 "NULL"），
  与在线查询 ``data_query.get_latest_record`` 的分表算法一致，保证查得到。

运行：
    常驻轮询：  python tasks/task_f09591_loss.py
    补跑某天：  python tasks/task_f09591_loss.py --date 20261110
    强制重导：  python tasks/task_f09591_loss.py --date 20261110 --force-reload
"""
import os
import re
import gc
import zlib
import sqlite3
import traceback
from typing import List

from .base_task import BaseSftpTask

from sftp_workdir_f09591.sftp_service import CONFIG as _SRC_CONFIG


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class LossTask(BaseSftpTask):
    TASK_NAME = "f09591_loss"
    FILE_PREFIXES = [_SRC_CONFIG["FILE_PREFIXES"]]                # ["F09591"]
    SFTP = {
        "host": _SRC_CONFIG["SFTP_HOST"],
        "port": _SRC_CONFIG["SFTP_PORT"],
        "username": _SRC_CONFIG["SFTP_USERNAME"],
        "password": _SRC_CONFIG["SFTP_PASSWORD"],
        "remote_path": _SRC_CONFIG["SFTP_REMOTE_PATH"],
    }
    DATA_EXPIRE_DAYS = _SRC_CONFIG["DATA_EXPIRE_DAYS"]            # 50
    LOCAL_FILE_EXPIRE_DAYS = _SRC_CONFIG["LOCAL_FILE_EXPIRE_DAYS"]  # 50
    BATCH_SIZE = _SRC_CONFIG["BATCH_SIZE"]                        # 5000
    NUM_TABLES = _SRC_CONFIG["NUM_TABLES"]                        # 128
    TABLE_PREFIX = _SRC_CONFIG["TABLE_NAME_PREFIX"]               # data_F09591
    POLL_INTERVAL_SECONDS = 1800                                 # 30 分钟一轮（文件大，放慢）

    DB_SCHEMA = [
        ("op_time", "TEXT"),
        ("user_id", "TEXT"),
        ("product_no", "TEXT"),
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
                cur.execute(f"CREATE TABLE IF NOT EXISTS {t} ({cols})")
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{t}_product_time "
                    f"ON {t} (product_no, op_time)"
                )
            conn.commit()
        finally:
            conn.close()

    # ---------- 续行重组 + 入库 ----------
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
            buffer = None  # 正在拼接的逻辑记录 [op_time,user_id,product_no,city_id,strategy_name]

            with open(avl_local_path, "r", encoding="utf-8", errors="ignore") as f:
                self.log.info("开始流式解析（按日期行重组续行）...")
                for line in f:
                    # 去掉行尾换行；源文件续行标记（单个 ASCII 反斜杠）只剥一个。
                    # 注意：不能用 str.rstrip('\\')，它会把 GBK 编码时尾字节为 0x5C
                    # 的汉字（如"乗"）误删一个字节，导致乱码。
                    content = _strip_eol(line)
                    if not content:
                        continue

                    if DATE_RE.match(content):
                        # 命中新逻辑记录起始：先把上一条落批
                        if buffer is not None:
                            self._route(buffer, batches)
                            total += 1
                            self._flush_full(cur, conn, batches)
                        buffer = self._split_head(content)
                        if buffer is None:
                            self.log.warning(f"跳过格式严重错误行: {content[:30]}...")
                    else:
                        # 续行：拼回上一记录的第 5 列，还原换行
                        if buffer is not None:
                            buffer[4] += "\n" + content
                        # 文件开头即出现续行（无上文）→ 丢弃

                if buffer is not None:
                    self._route(buffer, batches)
                    total += 1

            self._flush_remaining(cur, conn, batches)
            self.log.info(f"{os.path.basename(avl_local_path)} 解析完成，共 {total} 条逻辑记录")
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
                    f"INSERT INTO {t} ({', '.join(self.DB_COLUMNS)}) "
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
                    f"INSERT INTO {t} ({', '.join(self.DB_COLUMNS)}) "
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


def _strip_eol(line: str) -> str:
    """去掉物理行行尾换行符与至多一个 ASCII 续行反斜杠（见类 docstring 说明）。"""
    content = line.rstrip("\n")
    if content.endswith("\\"):
        content = content[:-1]
    return content
