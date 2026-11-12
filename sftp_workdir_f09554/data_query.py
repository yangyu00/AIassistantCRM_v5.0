import os
import sqlite3

# ==================== 配置区域 ====================
# 默认路径：与 db_query.py 同级的目录下的 sqlite_db 文件夹
_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sqlite_db')

# product_id 到表名的映射
PRODUCT_TABLE_MAP = {
    "202608041615335173": "data_F09554",  # 号卡业务
    "202608041615335172": "data_F09555",  # 套餐业务
    "202608041615335174": "data_F09556",  # 宽带业务
}


# 数据库文件前缀映射（表名 data_F09554 -> 文件名 F09554.db）
def _get_db_file_prefix(table_name):
    """从表名提取前缀，如 data_F09554 -> F09554"""
    return table_name.replace("data_", "")


# 数据库列名（与 sftp_service.py 中 DB_SCHEMA 保持一致）
DB_COLUMNS = [
    "op_time", "call_time", "product_no", "type_dl", "type_xl",
    "content_abstract", "cust_demand", "rec_strategy", "reject_reason",
    "reason_note", "refer_strategy", "city_id"
]


async def get_latest_record(telnum, product_id):
    """
    根据 telnum 查询数据库中日期最近的一条记录，返回字典。
    若 telnum 未映射或查询失败则返回空字典 {}。

    :param telnum: 电话号码（对应 product_no 字段）
    :param product_id: 产品编码
    :return: dict {列名: 列值}，未找到时返回 {}
    """
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

        # 按 op_time 降序排列，取第一条（日期最近的记录）
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
