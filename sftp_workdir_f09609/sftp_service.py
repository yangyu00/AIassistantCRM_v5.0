
import argparse
import gc
import os
import re
import sqlite3
import sys
import traceback
import zlib
from datetime import datetime, timedelta
from typing import List
import paramiko

# ================= 配置区域 =================
CONFIG = {
    'SFTP_HOST': '134.80.199.252',
    'SFTP_PORT': 22,
    'SFTP_USERNAME': 'wangsi',
    'SFTP_PASSWORD': 'W_s173O',
    'SFTP_REMOTE_PATH': "/ftpdata2/data_reverse/wangsi/aifile",
    'BATCH_SIZE': 5000,
    'FILE_PREFIXES': "F09609",
    'DATA_EXPIRE_DAYS': 50,  # 数据库数据保留天数
    'TABLE_NAME_PREFIX': f"data_F09609",
    'NUM_TABLES': 128,
    'LOCAL_FILE_EXPIRE_DAYS': 50,  # 本地AVL文件保留天数
    # --- 新增路径配置 ---
    'LOCAL_DIR_NAME': "files",  # 本地文件下载目录名
    'DB_DIR_NAME': "sqlite_db"  # 本地数据库存储目录名
}

# ================= 业务字段定义 =================
# 1. 更新数据库Schema，匹配新的5个字段
DB_SCHEMA = [
    ("op_time", "TEXT"),
    ("user_id", "TEXT"),
    ("product_no", "TEXT NOT NULL"),
    ("product_id", "TEXT NOT NULL"),
    ("strategy_script", "TEXT"),
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


def cleanup_expired_data(db_path: str, table_name_list: List[str]):
    """删除表中 op_time 超过配置过期时间的数据"""
    conn = None
    try:
        if not os.path.exists(db_path):
            return
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        expire_date = (datetime.now() - timedelta(days=CONFIG['DATA_EXPIRE_DAYS'])).strftime('%Y-%m-%d')
        for table_name in table_name_list:
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
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def parse_avl_and_save_to_sqlite(avl_path: str, db_path: str, table_name_list: List[str]):
    """流式解析宽表 AVL 并入库。
    每条逻辑记录以行首日期为边界，含 103 列 tab 分隔：
    前 3 列 = 日期 / user_id / 电话号(product_no)，第 4 列起两两一组 = (product_id, strategy_script)，共 50 组。
    策略信息含裸换行：先用日期正则缓冲整条逻辑记录的所有物理行，以换行符还原后再整体按 tab 切分成多条记录。
    """
    conn = None
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA cache_size = -64000")
        cursor.execute("PRAGMA temp_store = MEMORY")
        for table_name in table_name_list:
            columns_def = ", ".join([f"{col} {dtype}" for col, dtype in DB_SCHEMA])
            # product_no + product_id 联合主键：主键即唯一约束，重复入库时由 INSERT OR REPLACE 覆盖
            create_table_sql = (
                f"CREATE TABLE IF NOT EXISTS {table_name} "
                f"({columns_def}, PRIMARY KEY (product_no, product_id))"
            )
            cursor.execute(create_table_sql)
        conn.commit()
        batches = {t: [] for t in table_name_list}
        total_record_count = 0
        date_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}')
        current_logical_lines = None
        print("INFO:: 开始流式解析宽表文件(处理裸换行)...", flush=True)
        with open(avl_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line_content = line.rstrip('\n').rstrip('\\')
                if not line_content:
                    continue
                if date_pattern.match(line_content):
                    if current_logical_lines is not None:
                        total_record_count += _flush_logical_line(current_logical_lines, batches)
                        _check_and_commit_batches(batches, cursor, conn)
                    current_logical_lines = [line_content]
                else:
                    if current_logical_lines is not None:
                        current_logical_lines.append(line_content)
        if current_logical_lines is not None:
            total_record_count += _flush_logical_line(current_logical_lines, batches)
        _commit_remaining_batches(batches, cursor, conn)
        print(f"SUCCESS:: 数据追加完成，本次共展开入库 {total_record_count} 条记录。", flush=True)
    except Exception as e:
        print(f"ERROR:: 处理失败: {e}\n{traceback.format_exc()}", flush=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


# 辅助函数：把一条逻辑记录(可含裸换行的多物理行)展开为多条 5 字段记录入批
def _flush_logical_line(physical_lines: List[str], batches: dict) -> int:
    logical = "\n".join(physical_lines)
    parts = logical.split("\t")
    if len(parts) < 3:
        print(f"WARNING:: 列数不足，跳过: {logical[:30]}...", flush=True)
        return 0
    op_time, user_id, product_no = parts[0], parts[1], parts[2]
    rest = parts[3:]
    p_no = product_no if product_no else "NULL"
    table_index = zlib.crc32(p_no.encode("utf-8")) % CONFIG["NUM_TABLES"]
    target_table = list(batches.keys())[table_index]
    count = 0
    for i in range(0, len(rest) - 1, 2):
        product_id = rest[i]
        strategy_script = rest[i + 1]
        if not product_id.strip():
            continue
        batches[target_table].append((op_time, user_id, product_no, product_id, strategy_script))
        count += 1
    return count


# 以下为辅助函数（采用 INSERT OR REPLACE：联合主键存在则整行覆盖）
def _check_and_commit_batches(batches: dict, cursor, conn):
    """检查批次大小，如果满了则写入数据库"""
    for t_name, batch in batches.items():
        if len(batch) >= CONFIG['BATCH_SIZE']:
            insert_sql = f"INSERT OR REPLACE INTO {t_name} ({', '.join(DB_COLUMNS)}) VALUES (?, ?, ?, ?, ?)"
            cursor.executemany(insert_sql, batch)
            conn.commit()
            print(f"PROCESS:: 表 {t_name} 已写入 {len(batch)} 条", flush=True)
            batch.clear()


def _commit_remaining_batches(batches: dict, cursor, conn):
    """提交所有剩余未满批次的数据"""
    for t_name, batch in batches.items():
        if batch:
            insert_sql = f"INSERT OR REPLACE INTO {t_name} ({', '.join(DB_COLUMNS)}) VALUES (?, ?, ?, ?, ?)"
            cursor.executemany(insert_sql, batch)
            conn.commit()
            print(f"PROCESS:: 表 {t_name} 最终提交 {len(batch)} 条", flush=True)
            batch.clear()


# ================= 主流程控制 =================
def run_process(target_date: datetime, perform_cleanup: bool, force_reload: bool):
    """
    主执行逻辑
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
        remote_filename = f"{CONFIG['FILE_PREFIXES']}{date_str}000000.AVL"
        remote_path = f"{CONFIG['SFTP_REMOTE_PATH']}/{remote_filename}"
        local_path = os.path.join(local_dir, remote_filename)
        db_path = os.path.join(db_dir, f"{CONFIG['FILE_PREFIXES']}.db")
        table_name_list = [f"{CONFIG['TABLE_NAME_PREFIX']}_{i}" for i in range(CONFIG['NUM_TABLES'])]
        file_exists = os.path.exists(local_path)
        # --- 检查本地文件是否存在 ---
        if file_exists and not force_reload:
            print(f"SKIP:: 本地文件已存在，跳过下载与入库: {remote_filename}", flush=True)
            # 即使文件已存在，如果执行清理，也应检查数据库
            if perform_cleanup:
                cleanup_expired_data(db_path, table_name_list)
            return
        if not file_exists:
            try:
                sftp.stat(remote_path)
                print(f"INFO:: 正在下载 {remote_filename}...", flush=True)
                sftp.get(remote_path, local_path)
            except FileNotFoundError:
                print(f"WARNING:: 远程文件不存在: {remote_filename}", flush=True)
                return  # 远程文件不存在，直接返回，不再执行后续步骤
            except Exception as e:
                print(f"ERROR:: 处理文件出错: {e}", flush=True)
                return
        else:
            print(f"INFO:: 本地文件已存在，跳过下载，执行入库操作: {remote_filename}", flush=True)
        # 1. 下载完立即处理 (追加模式)
        # 如果是强制重载，也会执行到这里
        parse_avl_and_save_to_sqlite(local_path, db_path, table_name_list)
        # 2. 只有 perform_cleanup 为 True 时才清理数据
        if perform_cleanup:
            cleanup_expired_data(db_path, table_name_list)
        else:
            print(f"INFO:: 跳过数据库清理 (非默认模式)", flush=True)
    except Exception as e:
        print(f"ERROR:: SFTP 连接失败: {e}", flush=True)
    finally:
        if sftp:
            sftp.close()
        if ssh:
            ssh.close()
        # 3. 只有 perform_cleanup 为 True 时才清理本地文件
        if perform_cleanup:
            cleanup_local_expired_files(local_dir)
        else:
            print(f"INFO:: 跳过本地文件清理 (非默认模式)", flush=True)
    gc.collect()


def main():
    parser = argparse.ArgumentParser(description="SFTP 文件下载与入库工具")
    parser.add_argument('--date', type=str, help='指定处理日期 (格式: YYYYMMDD)，不传则默认为当天')
    # 新增参数：强制重新入库
    parser.add_argument('--force-reload', action='store_true', help='本地文件存在时，不重新下载，但强制重新入库')
    args = parser.parse_args()
    # 逻辑判断
    if args.date:
        try:
            target_date = datetime.strptime(args.date, '%Y%m%d')
            # 指定日期时，默认不执行清理，除非显式指定了 force-reload
            perform_cleanup = False
            print(f"MODE:: 指定日期模式 [{args.date}] - 不执行清理", flush=True)
        except ValueError:
            print("ERROR:: 日期格式错误，请使用 YYYYMMDD 格式", flush=True)
            sys.exit(1)
    else:
        target_date = datetime.now()
        perform_cleanup = True
        print(f"MODE:: 默认模式 [当天] - 执行清理", flush=True)
    run_process(target_date, perform_cleanup, args.force_reload)


if __name__ == '__main__':
    main()