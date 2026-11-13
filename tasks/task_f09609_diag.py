# -*- coding: utf-8 -*-
"""任务3：诊断 AVL 定时拉取与入库 —— 独立进程版。

对应原 Missions.md「任务3」，前缀 F09609，**拆分后约 3 亿条**，宽表结构 + 分表 + 续行。
从 ``sftp_workdir_f09609/sftp_service.py`` 抽取，规则与原实现完全一致：

【文件结构】
  远程文件 ``F09609{YYYYMMDD}000000.AVL``，UTF-8，制表符分隔。
  一条**逻辑记录**由「行首为日期」的物理行起始，其后若干续行同属该逻辑记录
  （strategy_script 正文含裸换行）。拼回后按 tab 展开为：

      第 1 列 op_time      日期
      第 2 列 user_id      用户标识
      第 3 列 product_no   手机号
      第 4 列起【两两一组】(product_id, strategy_script)，共约 50 组 → 最多 103 列

  即一个逻辑记录会**炸开成多条入库记录**（一个手机号对应多个产品各一条）。

【续行拼接规则（关键）】
  1. 逐物理行读，行首匹配 ``YYYY-MM-DD`` 者为新逻辑记录起点；
  2. 非日期开头的物理行 = 续行，追加进当前逻辑记录的物理行缓冲；
  3. 一个逻辑记录收集完（遇到下一个日期行或 EOF）后，把所有物理行用 ``\\n``
     join 还原完整文本，再整体 ``split('\\t')``——这样即使 tab 被换行拆开也能正确切列；
  4. 每个物理行行尾的反斜杠 ``\\`` 续行标记 rstrip 掉；
  5. 列数 < 3（连 op_time/user_id/product_no 都不全）→ 判定损坏，跳过整条。

【展开与分表】
  * 从第 4 列起，每两列 (product_id, strategy_script) 生成一条记录；
  * product_id 为空（空白）的组跳过，不入库；
  * 路由键同样 = ``zlib.crc32(product_no) % 128``（空则用 "NULL"），
    与在线查询 ``data_query.get_record`` 的分表算法一致；
  * 表建 ``PRIMARY KEY (product_no, product_id)``，入库用 ``INSERT OR REPLACE``，
    重跑/补导同一文件时天然幂等，不会产生重复行。

运行：
    常驻轮询：  python tasks/task_f09609_diag.py
    补跑某天：  python tasks/task_f09609_diag.py --date 20261110
    强制重导：  python tasks/task_f09609_diag.py --date 20261110 --force-reload
"""
import os
import re
import gc
import zlib
import sqlite3
import traceback
from typing import List

from .base_task import BaseSftpTask

from sftp_workdir_f09609.sftp_service import CONFIG as _SRC_CONFIG


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class DiagTask(BaseSftpTask):
    TASK_NAME = "f09609_diag"
    FILE_PREFIXES = [_SRC_CONFIG["FILE_PREFIXES"]]                # ["F09609"]
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
    TABLE_PREFIX = _SRC_CONFIG["TABLE_NAME_PREFIX"]               # data_F09609
    POLL_INTERVAL_SECONDS = 3600                                 # 60 分钟一轮（量最大）

    DB_SCHEMA = [
        ("op_time", "TEXT"),
        ("user_id", "TEXT"),
        ("product_no", "TEXT NOT NULL"),
        ("product_id", "TEXT NOT NULL"),
        ("strategy_script", "TEXT"),
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
                # 联合主键即唯一约束：重复入库由 INSERT OR REPLACE 覆盖
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {t} "
                    f"({cols}, PRIMARY KEY (product_no, product_id))"
                )
            conn.commit()
        finally:
            conn.close()

    # ---------- 续行重组 + 两两展开 + 入库 ----------
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
            logical_lines = None  # 当前逻辑记录的物理行缓冲

            with open(avl_local_path, "r", encoding="utf-8", errors="ignore") as f:
                self.log.info("开始流式解析宽表（日期行定界 + 物理行 join 还原）...")
                for line in f:
                    # 去掉行尾换行；续行标记（单个 ASCII 反斜杠）只剥一个。
                    # 不能用 rstrip('\\')：会误删 GBK 尾字节为 0x5C 的汉字。
                    content = _strip_eol(line)
                    if not content:
                        continue

                    if DATE_RE.match(content):
                        # 新逻辑记录起点：先把上一条展开入批
                        if logical_lines is not None:
                            total += self._expand(logical_lines, batches)
                            self._flush_full(cur, conn, batches)
                        logical_lines = [content]
                    else:
                        if logical_lines is not None:
                            logical_lines.append(content)
                        # 文件头就出现续行 → 无上文，丢弃

                if logical_lines is not None:
                    total += self._expand(logical_lines, batches)

            self._flush_remaining(cur, conn, batches)
            self.log.info(f"{os.path.basename(avl_local_path)} 解析完成，展开入库 {total} 条")
        except Exception as e:
            self.log.error(f"处理失败: {e}\n{traceback.format_exc()}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
            gc.collect()

    def _expand(self, physical_lines: List[str], batches: dict) -> int:
        """把一个逻辑记录的多个物理行还原为完整文本，按 tab 切列后两两展开。返回新增条数。"""
        logical = "\n".join(physical_lines)
        parts = logical.split("\t")
        if len(parts) < 3:
            self.log.warning(f"列数不足，跳过: {logical[:30]}...")
            return 0

        op_time, user_id, product_no = parts[0], parts[1], parts[2]
        rest = parts[3:]
        p_no = product_no if product_no else "NULL"
        idx = zlib.crc32(p_no.encode("utf-8")) % self.NUM_TABLES
        target = self._tables[idx]

        count = 0
        for i in range(0, len(rest) - 1, 2):
            product_id = rest[i]
            strategy_script = rest[i + 1]
            if not product_id.strip():
                continue  # 空产品位跳过
            batches[target].append((op_time, user_id, product_no, product_id, strategy_script))
            count += 1
        return count

    def _flush_full(self, cur, conn, batches: dict):
        sql_tpl = (
            "INSERT OR REPLACE INTO {t} ({cols}) VALUES ({ph})"
        )
        cols = ", ".join(self.DB_COLUMNS)
        ph = ", ".join("?" for _ in self.DB_COLUMNS)
        for t, batch in batches.items():
            if len(batch) >= self.BATCH_SIZE:
                cur.executemany(sql_tpl.format(t=t, cols=cols, ph=ph), batch)
                conn.commit()
                self.log.info(f"表 {t} 写入 {len(batch)} 条")
                batch.clear()

    def _flush_remaining(self, cur, conn, batches: dict):
        cols = ", ".join(self.DB_COLUMNS)
        ph = ", ".join("?" for _ in self.DB_COLUMNS)
        for t, batch in batches.items():
            if batch:
                cur.executemany(
                    f"INSERT OR REPLACE INTO {t} ({cols}) VALUES ({ph})", batch
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
    DiagTask.main()

def _strip_eol(line: str) -> str:
    """去掉物理行行尾换行符与至多一个 ASCII 续行反斜杠。"""
    content = line.rstrip("\n")
    if content.endswith("\\"):
        content = content[:-1]
    return content
