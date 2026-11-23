"""SQLite 落库层：只负责 I/O，不持有任何全局计数状态。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import warn

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS call_sum (
    date       TEXT    PRIMARY KEY,              -- 日期，格式: YYYY-MM-DD
    call_count INTEGER NOT NULL DEFAULT 0        -- 当日调用量
)
"""

_RETRIES = 4
_BACKOFF = 0.03


def _close(conn):
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _open(db_path, memory_journal: bool = False):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        # 先落一个 0 字节文件：部分挂载上 SQLite 自己「首次创建」会瞬时 I/O 错误
        db_path.touch()
    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        if memory_journal:
            conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute(TABLE_SQL)
        return conn
    except Exception:
        _close(conn)
        raise


def connect(db_path, *, force_memory_journal: bool = False):
    """建表（若不存在）并返回连接。"""
    last_error = None
    for attempt in range(_RETRIES):
        try:
            return _open(db_path,
                         memory_journal=(force_memory_journal or attempt > 0))
        except sqlite3.OperationalError as e:
            last_error = e
            time.sleep(_BACKOFF)
    raise last_error


def read_day(db_path, date_str) -> int:
    """读单日已落库量。"""
    conn = None
    try:
        conn = connect(db_path)
        row = conn.execute(
            "SELECT call_count FROM call_sum WHERE date = ?", (date_str,)).fetchone()
        return row[0] if row else 0
    except Exception as e:
        warn(f"调用量读取失败，按 0 处理: {e}")
        return 0
    finally:
        _close(conn)


def add_delta(db_path, date_str, delta) -> bool:
    """把增量累加进 SQLite。"""
    if not delta:
        return True
    last_error = None
    use_memory_journal = False
    for _ in range(_RETRIES):
        conn = None
        committed = False
        try:
            conn = connect(db_path, force_memory_journal=use_memory_journal)
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE call_sum SET call_count = call_count + ? WHERE date = ?",
                (delta, date_str))
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO call_sum(date, call_count) VALUES (?, ?)",
                    (date_str, delta))
            conn.commit()
            committed = True
            return True
        except Exception as e:
            last_error = e
            if committed:
                # commit 已成功，只剩收尾异常：不能重试，否则会重复累加
                return True
            use_memory_journal = True
            time.sleep(_BACKOFF)
        finally:
            _close(conn)
    warn(f"调用量落库失败（保留 {delta} 次待重试）: {last_error}")
    return False


def read_month(db_path, month) -> dict:
    """整月已落库量。"""
    conn = None
    try:
        conn = connect(db_path)
        rows = conn.execute(
            "SELECT date, call_count FROM call_sum WHERE substr(date, 1, 7) = ?",
            (month,)).fetchall()
        return dict(rows)
    except Exception as e:
        warn(f"调用量月查询读取失败，按 0 处理: {e}")
        return {}
    finally:
        _close(conn)
