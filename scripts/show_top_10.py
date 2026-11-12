import sqlite3

# ================== 配置区域 ==================
# 1. 直接在这里修改你的数据库文件完整路径
DB_PATH = r'./sqlite_db/your_database_filename.db'

# 2. 直接在这里修改你要查询的表名
TABLE_NAME = 'table_0'


# =============================================

def query_top_10():
    try:
        # 连接数据库
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # 执行查询 (直接取前10条，不排序)
        # 如果想按时间倒序取最新的10条，请改为: SELECT * FROM ... ORDER BY op_time DESC LIMIT 10
        query = f"SELECT * FROM {TABLE_NAME} LIMIT 10"
        cursor.execute(query)

        rows = cursor.fetchall()

        if not rows:
            print(f"表 {TABLE_NAME} 中没有数据。")
        else:
            print(f"\n查询到 {len(rows)} 条数据：\n")
            # 直接打印原始元组数据
            for row in rows:
                print(row)

        conn.close()

    except Exception as e:
        print(f"发生错误: {e}")


if __name__ == "__main__":
    query_top_10()