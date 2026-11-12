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
    'FILE_PREFIXES': "F09591",
    'DATA_EXPIRE_DAYS': 50,  # 数据库数据保留天数
    'TABLE_NAME_PREFIX': f"data_F09591",
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
    ("product_no", "TEXT"),
    ("city_id", "TEXT"),
    ("strategy_name", "TEXT"),
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
    """
    流式解析包含裸换行的 AVL 文件并存入 SQLite
    核心逻辑：通过检测行首日期格式来区分“新行”和“续行”
    """
    conn = None
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # 性能优化
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA cache_size = -64000")
        cursor.execute("PRAGMA temp_store = MEMORY")

        # 1. 预建表
        for table_name in table_name_list:
            columns_def = ", ".join([f"{col} {dtype}" for col, dtype in DB_SCHEMA])
            create_table_sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({columns_def})"
            cursor.execute(create_table_sql)

            # --- 新增：创建复合索引 ---
            # 索引命名规范通常为 idx_表名_列名
            index_name = f"idx_{table_name}_product_time"
            create_index_sql = f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} (product_no, op_time)"
            cursor.execute(create_index_sql)
            # -------------------------

        conn.commit()

        # 2. 初始化批处理列表
        batches: dict[str, List[tuple]] = {t: [] for t in table_name_list}
        total_row_count = 0

        # 3. 正则定义：匹配以日期开头的行 (例如 2026-09-18)
        # 注意：这里假设 op_time 是字段的第一列
        date_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}')

        # 4. 状态变量
        current_row_buffer = None  # 用于暂存正在拼接的行

        # 5. 逐行读取文件
        with open(avl_path, 'r', encoding='utf-8', errors='ignore') as f:
            print(f"INFO:: 开始流式解析文件 (处理裸换行)...", flush=True)
            for line in f:
                # 去掉行尾换行符，保留行内换行
                line_content = line.rstrip('\n').rstrip('\\')
                if not line_content:
                    continue

                # --- 核心判断逻辑 ---
                if date_pattern.match(line_content):
                    # 情况 A: 遇到新行 (以日期开头)
                    # 1. 如果 buffer 里有旧数据，先保存入库
                    if current_row_buffer is not None:
                        _save_row_to_batch(current_row_buffer, batches, CONFIG, total_row_count)
                        total_row_count += 1
                    # 检查批次提交
                    _check_and_commit_batches(batches, cursor, conn)

                    # 2. 解析新行的前几列
                    # 注意：这里 split 可能会因为最后一列有换行而被截断，这是正常的
                    parts = line_content.split('\t')
                    # 安全处理：确保至少有5个位置，防止索引越界
                    # 假设前4列是元数据，第5列开始是可能包含换行的内容
                    # 如果你的文件结构是固定的5列，且第5列可能为空，这里需要小心处理
                    if len(parts) >= 5:
                        # 标准情况：前4列完整，第5列是内容开头
                        op_time, user_id, product_no, city_id = parts[0], parts[1], parts[2], parts[3]
                        strategy_name = parts[4]
                    elif len(parts) == 4:
                        # 特殊情况：第5列为空，或者换行符紧跟在tab后面
                        op_time, user_id, product_no, city_id = parts[0], parts[1], parts[2], parts[3]
                        strategy_name = ""
                    else:
                        # 数据严重损坏，跳过
                        print(f"WARNING:: 跳过格式严重错误行: {line_content[:20]}...", flush=True)
                        current_row_buffer = None
                        continue

                    # 3. 初始化 Buffer
                    current_row_buffer = [op_time, user_id, product_no, city_id, strategy_name]
                else:
                    # 情况 B: 续行 (不是日期开头，属于上一行的最后一列内容)
                    if current_row_buffer is not None:
                        # 将内容拼接到最后一列 (strategy_name)，并恢复换行符
                        current_row_buffer[4] += '\n' + line_content
                    else:
                        # 边缘情况：文件开头就是乱码或续行，直接忽略
                        pass

        # 6. 文件读取完毕，处理最后一条残留数据
        if current_row_buffer is not None:
            _save_row_to_batch(current_row_buffer, batches, CONFIG, total_row_count)
            total_row_count += 1

        # 7. 提交所有剩余批次
        _commit_remaining_batches(batches, cursor, conn)
        print(f"SUCCESS:: 数据追加完成，本次共处理 {total_row_count} 条记录。", flush=True)

    except Exception as e:
        print(f"ERROR:: 处理失败: {e}\n{traceback.format_exc()}", flush=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


# --- 辅助函数：用于保持代码整洁 ---
def _save_row_to_batch(row_data: List[str], batches: dict, config: dict, count: int):
    """将单行数据根据 Hash 规则放入对应的批次列表"""
    # 注意：row_data 顺序必须与 DB_SCHEMA 一致
    op_time, user_id, product_no, city_id, strategy_name = row_data
    # 计算目标表 (Hash取模依据变更为 product_no)
    # 防止 product_no 为空导致 hash 错误
    p_no = product_no if product_no else "NULL"
    table_index = zlib.crc32(p_no.encode('utf-8')) % config['NUM_TABLES']
    target_table = list(batches.keys())[table_index]  # 获取表名
    # 组装元组
    record = (op_time, user_id, product_no, city_id, strategy_name)
    batches[target_table].append(record)


def _check_and_commit_batches(batches: dict, cursor, conn):
    """检查批次大小，如果满了则写入数据库"""
    for t_name, batch in batches.items():
        if len(batch) >= CONFIG['BATCH_SIZE']:
            insert_sql = f"INSERT INTO {t_name} ({', '.join(DB_COLUMNS)}) VALUES (?, ?, ?, ?, ?)"
            cursor.executemany(insert_sql, batch)
            conn.commit()
            print(f"PROCESS:: 表 {t_name} 已写入 {len(batch)} 条", flush=True)
            batch.clear()


def _commit_remaining_batches(batches: dict, cursor, conn):
    """提交所有剩余未满批次的数据"""
    for t_name, batch in batches.items():
        if batch:
            insert_sql = f"INSERT INTO {t_name} ({', '.join(DB_COLUMNS)}) VALUES (?, ?, ?, ?, ?)"
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
