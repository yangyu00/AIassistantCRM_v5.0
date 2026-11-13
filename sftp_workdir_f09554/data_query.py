import os
import sqlite3

from tasks.config import db_dir

PRODUCT_TABLE_MAP = {
    "202608041615335173": "data_F09554",  # 号卡业务
    "202608041615335172": "data_F09555",  # 套餐业务
    "202608041615335174": "data_F09556",  # 宽带业务
}

_DB_DIR = str(db_dir("f09554_opp"))

# 与 tasks/task_f09554_opp.py 的 DB_SCHEMA 保持一致
DB_COLUMNS = [
    "op_time", "call_time", "product_no", "type_dl", "type_xl",
    "content_abstract", "cust_demand", "rec_strategy", "reject_reason",
    "reason_note", "refer_strategy", "city_id"
]


def _get_db_file_prefix(table_name):
    return table_name.replace("data_", "")


async def get_latest_record(telnum, product_id):
    """按 telnum 查询日期最近的一条记录；未映射或查询失败返回 {}。"""
    table_name = PRODUCT_TABLE_MAP.get(product_id)
    if not table_name:
        print(f"WARNING:: product_id [{product_id}] 未在 PRODUCT_TABLE_MAP 中映射", flush=True)
        return {}

    db_path = os.path.join(_DB_DIR, f"{_get_db_file_prefix(table_name)}.db")
    if not os.path.exists(db_path):
        print(f"WARNING:: 数据库文件不存在: {db_path}", flush=True)
        return {}

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        query = f"SELECT * FROM {table_name} WHERE product_no = ? ORDER BY op_time DESC LIMIT 1"
        cursor.execute(query, (telnum,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(zip(DB_COLUMNS, row))
        return {}
    except Exception as e:
        print(f"ERROR:: 查询数据库失败: {e}", flush=True)
        return {}
