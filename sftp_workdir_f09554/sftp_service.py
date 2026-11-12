import os
import gc
import sys
import sqlite3

import paramiko
import argparse
import traceback
from datetime import datetime, timedelta

# ================= 配置区域 =================
CONFIG = {
    'SFTP_HOST': '134.80.199.252',
    'SFTP_PORT': 22,
    'SFTP_USERNAME': 'wangsi',
    'SFTP_PASSWORD': 'W_s173O',
    'SFTP_REMOTE_PATH': "/ftpdata2/data_reverse/wangsi/aifile",
    'BATCH_SIZE': 10000,
    'FILE_PREFIXES': ["F09554", "F09555", "F09556"],  # 号卡业务、套餐业务、宽带业务
    'DATA_EXPIRE_DAYS': 30,  # 数据库数据保留天数
    'LOCAL_FILE_EXPIRE_DAYS': 7,  # 本地AVL文件保留天数

    # --- 新增路径配置 ---
    'LOCAL_DIR_NAME': "files",  # 本地文件下载目录名
    'DB_DIR_NAME': "sqlite_db"  # 本地数据库存储目录名
}

# ================= 业务字段定义 =================
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
    ("city_id", "INTEGER")
]

DB_COLUMNS = [col[0] for col in DB_SCHEMA]


# ================= 核心功能函数 =================

def cleanup_local_expired_files(local_dir: str):
    """删除本地目录中超过指定天数的 .AVL 文件"""
    try:
        if not os.path.exists(local_dir):
            return
        expire_time = datetime.now() - timedelta(days=CONFIG['LOCAL_FILE_EXPIRE_DAYS'])
        deleted_count = 0
        for filename in os.listdir(local_dir):
            if filename.endswith('.AVL'):
                file_path = os.path.join(local_dir, filename)
                file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                if file_mtime < expire_time:
                    os.remove(file_path)
                    print(f"CLEANUP:: 已删除过期本地文件: {filename}", flush=True)
                    deleted_count += 1
        if deleted_count == 0:
            print(f"CLEANUP:: 本地文件目录无超过 {CONFIG['LOCAL_FILE_EXPIRE_DAYS']} 天的过期文件。", flush=True)
    except Exception as e:
        print(f"ERROR:: 清理本地过期文件失败: {e}", flush=True)


def cleanup_expired_data(db_path: str, table_name: str):
    """删除表中 op_time 超过配置过期时间的数据"""
    conn = None
    try:
        if not os.path.exists(db_path):
            return
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        expire_date = (datetime.now() - timedelta(days=CONFIG['DATA_EXPIRE_DAYS'])).strftime('%Y-%m-%d')
        delete_sql = f"DELETE FROM {table_name} WHERE op_time < ?"
        cursor.execute(delete_sql, (expire_date,))
        deleted_count = cursor.rowcount
        conn.commit()
        if deleted_count > 0:
            print(f"CLEANUP:: 表 {table_name} 已清理 {deleted_count} 条超过 {CONFIG['DATA_EXPIRE_DAYS']} 天的数据。",
                  flush=True)
        else:
            print(f"CLEANUP:: 表 {table_name} 无过期数据。", flush=True)
    except Exception as e:
        print(f"ERROR:: 清理过期数据失败: {e}", flush=True)
        if conn: conn.rollback()
    finally:
        if conn: conn.close()


def parse_avl_and_save_to_sqlite(avl_path: str, db_path: str, table_name: str):
    """流式解析AVL文件并存入 SQLite (追加模式)"""
    conn = None
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 性能优化
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = OFF")
        cursor.execute("PRAGMA cache_size = -64000")
        cursor.execute("PRAGMA temp_store = MEMORY")

        # 1. 建表
        columns_def = ", ".join([f"{col} {dtype}" for col, dtype in DB_SCHEMA])
        create_table_sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({columns_def})"
        cursor.execute(create_table_sql)

        # 2. 创建索引 (新增部分)
        # 为优化查询 `WHERE product_no = ? ORDER BY op_time DESC` 而创建复合索引
        index_name = f"idx_{table_name}_product_op"
        create_index_sql = f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} (product_no, op_time)"
        cursor.execute(create_index_sql)

        conn.commit()

        print(f"INFO:: 准备向表 {table_name} 追加数据...", flush=True)

        insert_sql = f"INSERT INTO {table_name} ({', '.join(DB_COLUMNS)}) VALUES ({', '.join(['?' for _ in DB_COLUMNS])})"
        batch_data = []
        row_count = 0

        # 尝试多种编码
        encodings = ['utf-8', 'gbk', 'latin-1']
        file_handle = None
        for enc in encodings:
            try:
                file_handle = open(avl_path, 'r', encoding=enc, errors='ignore')
                print(f"INFO:: 使用编码 {enc} 读取文件", flush=True)
                break
            except Exception:
                continue

        if not file_handle:
            raise Exception("无法识别文件编码")

        with file_handle as f:
            for line in f:
                line = line.strip()
                if not line: continue
                fields = line.split('\t')

                # 数据清洗
                if len(fields) < len(DB_COLUMNS):
                    fields.extend([''] * (len(DB_COLUMNS) - len(fields)))
                elif len(fields) > len(DB_COLUMNS):
                    fields = fields[:len(DB_COLUMNS)]

                # 特殊处理 city_id
                try:
                    val = fields[-1].strip()
                    fields[-1] = int(val) if val else 0
                except ValueError:
                    fields[-1] = 0

                batch_data.append(tuple(fields))
                row_count += 1

                if len(batch_data) >= CONFIG['BATCH_SIZE']:
                    cursor.executemany(insert_sql, batch_data)
                    conn.commit()
                    batch_data = []
                    print(f"PROCESS:: 已追加 {row_count} 条...", flush=True)

            # 提交剩余
            if batch_data:
                cursor.executemany(insert_sql, batch_data)
                conn.commit()

        print(f"SUCCESS:: 数据追加完成，本次共 {row_count} 条。", flush=True)

    except Exception as e:
        print(f"ERROR:: 处理失败: {e}\n{traceback.format_exc()}", flush=True)
        if conn: conn.rollback()
    finally:
        if conn: conn.close()


# ================= 主流程控制 =================

def run_process(target_date: datetime, perform_cleanup: bool):
    """
    主执行逻辑
    :param target_date: 处理的日期对象
    :param perform_cleanup: 是否执行清理逻辑
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # 从 CONFIG 中读取目录名并拼接完整路径
    local_dir: str = str(os.path.join(base_dir, CONFIG['LOCAL_DIR_NAME']))
    db_dir: str = str(os.path.join(base_dir, CONFIG['DB_DIR_NAME']))

    os.makedirs(local_dir, exist_ok=True)
    os.makedirs(db_dir, exist_ok=True)

    date_str = target_date.strftime('%Y%m%d')
    print(f"INFO:: 开始处理日期: {date_str}", flush=True)

    ssh = None
    sftp = None
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(CONFIG['SFTP_HOST'], CONFIG['SFTP_PORT'], CONFIG['SFTP_USERNAME'], CONFIG['SFTP_PASSWORD'])
        sftp = ssh.open_sftp()
        print("INFO:: SFTP 连接成功", flush=True)

        for prefix in CONFIG['FILE_PREFIXES']:
            remote_filename = f"{prefix}{date_str}000000.AVL"
            remote_path = f"{CONFIG['SFTP_REMOTE_PATH']}/{remote_filename}"
            local_path = os.path.join(local_dir, remote_filename)
            db_path = os.path.join(db_dir, f"{prefix}.db")
            table_name = f"data_{prefix}"

            # --- 检查本地文件是否存在 ---
            if os.path.exists(local_path):
                print(f"SKIP:: 本地文件已存在，跳过下载与入库: {remote_filename}", flush=True)
                # 即使跳过处理，如果开启了清理模式，依然检查该表的数据清理
                # if perform_cleanup:
                #     cleanup_expired_data(db_path, table_name)
                continue
            # -----------------------------------

            try:
                sftp.stat(remote_path)
                print(f"INFO:: 正在下载 {remote_filename}...", flush=True)
                sftp.get(remote_path, local_path)

                # 1. 下载完立即处理 (追加模式)
                parse_avl_and_save_to_sqlite(local_path, db_path, table_name)

                # 2. 只有 perform_cleanup 为 True 时才清理数据
                if perform_cleanup:
                    cleanup_expired_data(db_path, table_name)
                else:
                    print(f"INFO:: 跳过数据库清理 (非默认模式): {table_name}", flush=True)

            except FileNotFoundError:
                print(f"WARNING:: 远程文件不存在: {remote_filename}", flush=True)
            except Exception as e:
                print(f"ERROR:: 处理文件出错: {e}", flush=True)

    except Exception as e:
        print(f"ERROR:: SFTP 连接失败: {e}", flush=True)
    finally:
        if sftp: sftp.close()
        if ssh: ssh.close()

    # 3. 只有 perform_cleanup 为 True 时才清理本地文件
    if perform_cleanup:
        cleanup_local_expired_files(local_dir)
    else:
        print(f"INFO:: 跳过本地文件清理 (非默认模式)", flush=True)

    gc.collect()


def main():
    parser = argparse.ArgumentParser(description="SFTP 文件下载与入库工具")
    parser.add_argument('--date', type=str, help='指定处理日期 (格式: YYYYMMDD)，不传则默认为当天')

    args = parser.parse_args()

    # 逻辑判断：如果不传参，默认为当天，且执行清理；如果传参，使用指定日期，不执行清理
    if args.date:
        try:
            target_date = datetime.strptime(args.date, '%Y%m%d')
            perform_cleanup = False
            print(f"MODE:: 指定日期模式 [{args.date}] - 不执行清理", flush=True)
        except ValueError:
            print("ERROR:: 日期格式错误，请使用 YYYYMMDD 格式", flush=True)
            sys.exit(1)
    else:
        target_date = datetime.now()
        perform_cleanup = True
        print(f"MODE:: 默认模式 [当天] - 执行清理", flush=True)

    run_process(target_date, perform_cleanup)


if __name__ == '__main__':
    main()
