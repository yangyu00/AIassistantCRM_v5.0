import os
import sqlite3
import zlib

from tasks.config import TASKS, db_dir

_CONF = TASKS["f09609_diag"]
_DB_DIR = str(db_dir("f09609_diag"))

# 与 tasks/task_f09609_diag.py 的 DB_SCHEMA 保持一致
DB_COLUMNS = ["op_time", "user_id", "product_no", "product_id", "strategy_script"]


async def get_record(product_no, product_id):
    """按 product_no + product_id 查询记录；查询失败返回 {}。"""
    p_no = product_no if product_no else "NULL"
    table_index = zlib.crc32(p_no.encode('utf-8')) % _CONF["NUM_TABLES"]
    table_name = f"{_CONF['TABLE_NAME_PREFIX']}_{table_index}"
    db_path = os.path.join(_DB_DIR, f"{_CONF['FILE_PREFIXES'][0]}.db")

    if not os.path.exists(db_path):
        print(f"WARNING:: 数据库文件不存在: {db_path}", flush=True)
        return {}

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        query = f"""
            SELECT * FROM {table_name}
            WHERE product_no = ? AND product_id = ?
            LIMIT 1
        """
        cursor.execute(query, (product_no, product_id))
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(zip(DB_COLUMNS, row))
        return {}
    except Exception as e:
        print(f"ERROR:: 查询数据库失败: {e}", flush=True)
        return {}
