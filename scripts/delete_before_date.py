# -*- coding: utf-8 -*-
"""删除指定日期之前（不含该日期）的调用量统计数据。

用法:
    python delete_before_date.py 2025-01-01
    -> 删除 call_sum.db 中日期小于 2025-01-01 的所有记录（2025-01-01 当天保留）

参数校验失败时脚本退出码为 2，正常删除返回 0。
"""
import sys
import sqlite3
import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / 'log' / 'call_sum.db'


def delete_before(date_str: str) -> int:
    """删除日期严格早于 date_str（YYYY-MM-DD）的记录，返回删除条数。"""
    # 校验日期格式，非法则抛出 ValueError
    datetime.datetime.strptime(date_str, '%Y-%m-%d')

    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute('DELETE FROM call_sum WHERE date < ?', (date_str,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def main():
    if len(sys.argv) != 2:
        print('用法: python delete_before_date.py <日期>  例: python delete_before_date.py 2025-01-01')
        print('说明: 删除该日期之前（不含该日期）的所有记录')
        sys.exit(2)

    date_str = sys.argv[1]
    try:
        deleted = delete_before(date_str)
    except ValueError:
        print(f'错误: 日期格式不正确 "{date_str}"，应为 YYYY-MM-DD，如 2025-01-01')
        sys.exit(2)

    print(f'已删除 {date_str} 之前的记录 {deleted} 条')
    sys.exit(0)


if __name__ == '__main__':
    main()
