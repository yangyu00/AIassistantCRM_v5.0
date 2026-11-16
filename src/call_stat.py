# -*- coding: utf-8 -*-
"""调用量统计：请求先在内存累计，达到阈值才写一次 SQLite。

数据库文件: log/call_sum.db
表结构: call_sum(date TEXT PRIMARY KEY, call_count INTEGER NOT NULL DEFAULT 0)

与逐请求写库的区别：未落库的增量暂存在进程内全局字典 ``_PENDING[date]``；
某日期累计到 ``flush_threshold`` 次才把这段增量一次性 UPDATE/INSERT 进
call_sum。查询接口会叠加 _PENDING，因此读到的始终是「已落库 + 未落库」的完整值。

阈值来自项目根 config.yaml 的 ``call_stat.flush_threshold``（默认 100），
也可在启动时调用 ``configure(flush_threshold=...)`` 覆盖。
"""
import atexit
import calendar
import datetime
import sqlite3
import threading
from pathlib import Path

from config import ImmutableYamlConfig

DB_PATH = Path(__file__).resolve().parent.parent / 'log' / 'call_sum.db'

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS call_sum (
    date       TEXT    PRIMARY KEY,              -- 日期，格式: YYYY-MM-DD
    call_count INTEGER NOT NULL DEFAULT 0        -- 当日调用量
)
"""

_DEFAULT_THRESHOLD = 100


def _load_threshold() -> int:
    cfg = ImmutableYamlConfig()
    section = cfg.get('call_stat', {}) or {}
    val = int(section.get('flush_threshold', _DEFAULT_THRESHOLD))
    if val <= 0:
        raise ValueError(f'call_stat.flush_threshold 必须为正整数，当前为 {val}')
    return val


FLUSH_THRESHOLD = _load_threshold()

# 按日期暂存「尚未落库」的调用次数增量；正常情况下只会有当天一项，
# 跨天时残留的旧日期会在后续请求里自动重试落库。
_LOCK = threading.RLock()
_PENDING = {}


def configure(flush_threshold: int = None) -> int:
    """覆盖落库阈值（启动时或测试时调用），返回生效值。"""
    global FLUSH_THRESHOLD
    if flush_threshold is not None:
        flush_threshold = int(flush_threshold)
        if flush_threshold <= 0:
            raise ValueError(f'flush_threshold 必须为正整数，当前为 {flush_threshold}')
        FLUSH_THRESHOLD = flush_threshold
    return FLUSH_THRESHOLD


def _today() -> str:
    return datetime.date.today().strftime('%Y-%m-%d')


def get_conn() -> sqlite3.Connection:
    """创建表（若不存在）并返回连接。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(TABLE_SQL)
    return conn


def _flush_date(date_str: str) -> bool:
    """把 date_str 的未落库增量写入 SQLite；成功才从 _PENDING 移除。"""
    delta = _PENDING.get(date_str, 0)
    if not delta:
        return True
    try:
        conn = get_conn()
        try:
            conn.execute('BEGIN IMMEDIATE')
            cur = conn.execute(
                'UPDATE call_sum SET call_count = call_count + ? WHERE date = ?',
                (delta, date_str))
            if cur.rowcount == 0:
                conn.execute(
                    'INSERT INTO call_sum(date, call_count) VALUES (?, ?)',
                    (date_str, delta))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # 保留增量，下一轮或进程退出时重试；绝不把异常抛给正在处理的请求
        print(f'WARNING:: 调用量落库失败（保留 {delta} 次待重试）: {e}', flush=True)
        return False
    _PENDING.pop(date_str, None)
    return True


def flush() -> dict:
    """把所有未落库增量写入 SQLite，返回仍待重试的增量（正常应为空）。"""
    with _LOCK:
        for d in list(_PENDING):
            _flush_date(d)
        return dict(_PENDING)


def record_call() -> int:
    """一次请求计数：内存 +1，达到阈值才写一次库，返回当天累计调用量。"""
    today = _today()
    with _LOCK:
        _PENDING[today] = _PENDING.get(today, 0) + 1
        if _PENDING[today] >= FLUSH_THRESHOLD:
            _flush_date(today)
        # 跨天残留（旧日期不足阈值就翻天）顺带重试，成功即从字典移除
        for d in [d for d in _PENDING if d != today]:
            _flush_date(d)
        return _read_day(today) + _PENDING.get(today, 0)


def _read_day(date_str: str) -> int:
    """读单日已落库量；库不可读时降级为 0，不让异常冒泡到请求。"""
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                'SELECT call_count FROM call_sum WHERE date = ?', (date_str,)).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()
    except Exception as e:
        print(f'WARNING:: 调用量读取失败，按 0 处理: {e}', flush=True)
        return 0


def get_call_count(date_str: str) -> int:
    """单日调用量 = 已落库 + 内存未落库。"""
    with _LOCK:
        return _read_day(date_str) + _PENDING.get(date_str, 0)


def get_month_counts(month: str):
    """整月每天的调用量，无记录补 0；返回 [(YYYY-MM-DD, count), ...]。"""
    year, mon = int(month[:4]), int(month[5:7])
    days = calendar.monthrange(year, mon)[1]
    with _LOCK:
        try:
            conn = get_conn()
            try:
                rows = conn.execute(
                    'SELECT date, call_count FROM call_sum WHERE substr(date, 1, 7) = ?',
                    (month,)).fetchall()
                persisted = dict(rows)
            finally:
                conn.close()
        except Exception as e:
            print(f'WARNING:: 调用量月查询读取失败，按 0 处理: {e}', flush=True)
            persisted = {}
        pending = dict(_PENDING)
    out = []
    for d in range(1, days + 1):
        date_str = f'{month}-{d:02d}'
        out.append((date_str, persisted.get(date_str, 0) + pending.get(date_str, 0)))
    return out


# atexit 作为其它运行方式的兜底；uvicorn 正常关闭由 main.py 的 shutdown 钩子
# 显式调用 flush（其 SIGTERM 退出路径不经过 atexit）。两处都是幂等的。
atexit.register(flush)


if __name__ == '__main__':
    configure(flush_threshold=3)
    for _ in range(7):
        print(f'当日累计 {record_call()} | 待落库 {dict(_PENDING)}')
    print('flush 后待落库:', flush())
