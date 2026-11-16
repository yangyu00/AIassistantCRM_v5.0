# -*- coding: utf-8 -*-
"""调用量统计：当天总数存进程内全局字典，达到阈值才写一次 SQLite。

数据库文件: log/call_sum.db
表结构: call_sum(date TEXT PRIMARY KEY, call_count INTEGER NOT NULL DEFAULT 0)

两个全局字典（RLock 保护）：
  ``_DAY_TOTAL[date]``  当天**完整累计调用量** = 库里已落库 + 本进程所有请求
                        （无论是否已落库）。请求路径只改这个变量，不读库。
  ``_PENDING[date]``    其中「尚未落库」的增量，累计到 flush_threshold 才
                        一次性 UPDATE/INSERT 进 call_sum 并清零。

重启后继续计数：表按 ``call_count = call_count + delta`` 累加，
``startup()``（lifespan 启动阶段）或首次 ``record_call()`` 会从库读回当天
已落库值作为 _DAY_TOTAL 基数，因此进程关闭再启动不会从 0 开始。

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

_LOCK = threading.RLock()
_DAY_TOTAL = {}   # date -> 当天完整累计（重启时从库恢复）
_PENDING = {}     # date -> 未落库增量


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


def _read_day(date_str: str) -> int:
    """读单日已落库量；库不可读时降级为 0，不让异常冒泡到请求或 lifespan。"""
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


def _ensure_day(date_str: str) -> int:
    """确保 _DAY_TOTAL 有该日期的基数；首次从库恢复，实现重启继续计数。

    lifespan 未执行时（例如 TestClient 不进上下文、直接 import 后手工调用）
    也会由首次 record_call() 触发，行为一致。
    """
    if date_str not in _DAY_TOTAL:
        _DAY_TOTAL[date_str] = _read_day(date_str) + _PENDING.get(date_str, 0)
    return _DAY_TOTAL[date_str]


def startup() -> int:
    """lifespan 启动阶段调用：确保表存在并从库恢复当天累计基数。

    返回启动时当天已累计的调用量，供日志确认「继续计数」而非重新计数。
    建表或读库失败只告警，不阻断服务启动。
    """
    with _LOCK:
        try:
            conn = get_conn()
            conn.close()
        except Exception as e:
            print(f'WARNING:: 调用量库初始化失败: {e}', flush=True)
        return _ensure_day(_today())


def _flush_date(date_str: str) -> bool:
    """把 date_str 的未落库增量写入 SQLite；成功才从 _PENDING 移除。

    _DAY_TOTAL 不受影响：它记的是完整累计，落库与否都不改变总数。
    """
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
    """把所有未落库增量写入 SQLite，返回仍待重试的增量（正常应为空）。幂等。"""
    with _LOCK:
        for d in list(_PENDING):
            _flush_date(d)
        return dict(_PENDING)


def record_call() -> int:
    """一次请求计数：内存 +1，达到阈值才写一次库，返回当天累计调用量。

    热路径完全不读库（除非恰好触发 flush），返回值直接来自 _DAY_TOTAL。
    """
    today = _today()
    with _LOCK:
        _ensure_day(today)
        _DAY_TOTAL[today] += 1
        _PENDING[today] = _PENDING.get(today, 0) + 1
        if _PENDING[today] >= FLUSH_THRESHOLD:
            _flush_date(today)
        # 跨天残留（旧日期不足阈值就翻天）顺带重试落库，成功即从字典移除
        for d in [d for d in _PENDING if d != today]:
            _flush_date(d)
        return _DAY_TOTAL[today]


def get_call_count(date_str: str) -> int:
    """单日调用量：本进程有活动直接用 _DAY_TOTAL，否则读库 + 未落库增量。"""
    with _LOCK:
        if date_str in _DAY_TOTAL:
            return _DAY_TOTAL[date_str]
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
        totals = dict(_DAY_TOTAL)
    out = []
    for d in range(1, days + 1):
        date_str = f'{month}-{d:02d}'
        if date_str in totals:
            # _DAY_TOTAL 已是完整累计，不能再叠加 pending
            count = totals[date_str]
        else:
            count = persisted.get(date_str, 0) + pending.get(date_str, 0)
        out.append((date_str, count))
    return out


# atexit 作为非 ASGI 运行方式的兜底；ASGI 正常关闭由 main.py 的 lifespan
# 在 yield 之后调用 flush（uvicorn 的 SIGTERM 退出路径不经过 atexit）。
# 两处都是幂等的。
atexit.register(flush)


if __name__ == '__main__':
    configure(flush_threshold=3)
    print('startup 恢复基数 =', startup())
    for _ in range(7):
        print(f'当日累计 {record_call()} | 待落库 {dict(_PENDING)}')
    print('flush 后待落库:', flush())
