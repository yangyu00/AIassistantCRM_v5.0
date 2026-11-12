# -*- coding: utf-8 -*-
"""将 call_sum.db 中的调用量统计数据导出为 CSV 文件。

默认导出到 log/call_sum.csv，也可指定输出路径:
    python export_call_csv.py                 # 导出到 log/call_sum.csv
    python export_call_csv.py out.csv         # 导出到指定文件

CSV 使用 utf-8-sig 编码（带 BOM），可直接用 Excel 打开，表头为: 日期, 调用量
"""
import sys
import csv
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / 'log' / 'call_sum.db'
DEFAULT_OUT_PATH = Path(__file__).resolve().parent.parent / 'log' / 'call_sum.csv'


def export_to_csv(out_path: Path) -> int:
    """导出全部记录（按日期升序）到 CSV，返回导出的记录条数。"""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            'SELECT date, call_count FROM call_sum ORDER BY date'
        ).fetchall()
    finally:
        conn.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['日期', '调用量'])
        writer.writerows(rows)

    return len(rows)


def main():
    if len(sys.argv) > 2:
        print('用法: python export_call_csv.py [输出文件路径]')
        sys.exit(2)

    out_path = Path(sys.argv[1]) if len(sys.argv) == 2 else DEFAULT_OUT_PATH

    try:
        count = export_to_csv(out_path)
    except sqlite3.OperationalError as e:
        print(f'错误: 导出失败（数据库可能不存在或表结构异常）: {e}')
        sys.exit(1)

    print(f'导出成功: 共 {count} 条记录 -> {out_path}')
    sys.exit(0)


if __name__ == '__main__':
    main()
