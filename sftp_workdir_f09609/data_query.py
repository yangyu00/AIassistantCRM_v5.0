import os
import sqlite3
import zlib

# 1. 导入 CONFIG 以获取分表配置 (NUM_TABLES, TABLE_NAME_PREFIX)
from .sftp_service import CONFIG

# ==================== 配置区域 ====================
# 默认路径：与 db_query.py 同级的目录下的 sqlite_db 文件夹
_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sqlite_db')

# 数据库列名（与 sftp_service.py 中 DB_SCHEMA 保持一致）
DB_COLUMNS = ["op_time", "user_id", "product_no", "product_id", "strategy_script"]


async def get_record(product_no, product_id):
    """
    根据 product_no product_id查询数据库中记录。
    :param product_no: 手机号
    :param product_id: 产品id
    :return: dict {列名: 列值}，未找到时返回 {}
    """
    p_no = product_no if product_no else "NULL"
    table_index = zlib.crc32(p_no.encode('utf-8')) % CONFIG['NUM_TABLES']

    table_name = f"{CONFIG['TABLE_NAME_PREFIX']}_{table_index}"

    db_path = os.path.join(_DB_DIR, f"{CONFIG['FILE_PREFIXES']}.db")

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
