# -*- coding: utf-8 -*-
"""调用量统计模块：使用 SQLite 在 log 目录下记录每日接口调用量。

数据库文件: log/call_sum.db
表结构: call_sum(date TEXT PRIMARY KEY, call_count INTEGER NOT NULL DEFAULT 0)
"""
import sqlite3
import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / 'log' / 'call_sum.db'

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS call_sum (
    date       TEXT    PRIMARY KEY,              -- 日期，格式: YYYY-MM-DD
    call_count INTEGER NOT NULL DEFAULT 0        -- 当日调用量
)
"""


def get_conn() -> sqlite3.Connection:
    """创建表（若不存在）并返回连接。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(TABLE_SQL)
    return conn


def record_call(today: str = None) -> int:
    """将指定日期（默认今天）的调用量 +1，返回更新后的当日调用量。

    date 为主键：当天首次调用插入记录，后续调用累加。
    """
    if today is None:
        today = datetime.date.today().strftime('%Y-%m-%d')

    conn = get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE call_sum SET call_count = call_count + 1 WHERE date = ?', (today,))
        if conn.total_changes == 0:
            conn.execute('INSERT INTO call_sum(date, call_count) VALUES (?, 1)', (today,))
        conn.commit()

        cur = conn.execute('SELECT call_count FROM call_sum WHERE date = ?', (today,))
        row = cur.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


if __name__ == '__main__':
    # 手动测试：连续记录 3 次今天的调用
    for _ in range(3):
        count = record_call()
        print(f'当前调用量: {count}')
