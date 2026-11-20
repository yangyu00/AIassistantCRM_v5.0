# -*- coding: utf-8 -*-
"""内存计数核心：两个全局字典 + RLock，热路径不读库（除非恰好触发 flush）。

``_DAY_TOTAL`` 记当天完整累计，``_PENDING`` 记尚未落库的增量；累计到阈值
才写一次库，写失败保留增量重试，跨天残留顺带补齐。``startup()`` 从库恢复
当天基数，lifespan 关闭与 ``atexit`` 两处 ``flush()`` 均为幂等。

阈值与库路径来自包内 ``.env``，首次调用时懒加载并冻结；``flush()`` 在无
pending 时提前返回，保证「仅 import」不产生任何 I/O。
"""
from __future__ import annotations

import atexit
import calendar
import datetime
import threading
from dataclasses import replace
from pathlib import Path

from . import store
from .config import CallStatConfig, RUNTIME_DIR, load_config, warn

_LOCK = threading.RLock()
_DAY_TOTAL = {}          # date -> 当天完整累计（重启时从库恢复）
_PENDING = {}            # date -> 尚未落库的增量
_CFG = None              # 首次使用时加载并冻结，import 零 I/O
_THRESHOLD_OVERRIDE = None  # configure() 覆盖值，供启动 / 自检使用


def _cfg() -> CallStatConfig:
    global _CFG
    if _CFG is None:
        with _LOCK:
            if _CFG is None:
                _CFG = load_config()
    return _CFG


def get_config() -> CallStatConfig:
    """返回本进程冻结的配置对象（首次调用时读一次包内 .env）。"""
    return _cfg()


def _threshold() -> int:
    return (_THRESHOLD_OVERRIDE if _THRESHOLD_OVERRIDE is not None
            else _cfg().flush_threshold)


def configure(flush_threshold: int = None, *, db_path=None) -> int:
    """覆盖落库阈值（启动时或自检时调用），返回生效值。

    ``db_path`` 仅用于自检把库隔离到 ``runtime/selftest/``；仍强制位于
    ``RUNTIME_DIR`` 内，不允许把产物写到模块目录之外。
    """
    global _CFG, _THRESHOLD_OVERRIDE
    cfg = _cfg()
    if flush_threshold is not None:
        n = int(flush_threshold)
        if n <= 0:
            raise ValueError(f"flush_threshold 必须为正整数，当前为 {n}")
        _THRESHOLD_OVERRIDE = n
    if db_path is not None:
        p = Path(db_path).resolve()
        base = RUNTIME_DIR.resolve()
        if p != base and base not in p.parents:
            raise ValueError(f"db_path 必须位于 {base} 内，当前为 {p}")
        cfg = replace(cfg, db_path=p)
        _CFG = cfg
    return _threshold()


def _today() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


def _ensure_day(date_str: str) -> int:
    """确保 _DAY_TOTAL 有该日期的基数；首次从库恢复，实现重启继续计数。

    lifespan 未执行时（例如 TestClient 不进上下文、直接 import 后手工调用）
    也会由首次 record_call() 触发，行为一致。
    """
    if date_str not in _DAY_TOTAL:
        _DAY_TOTAL[date_str] = (store.read_day(_cfg().db_path, date_str)
                                + _PENDING.get(date_str, 0))
    return _DAY_TOTAL[date_str]


def startup() -> int:
    """lifespan 启动阶段调用：确保表存在并从库恢复当天累计基数。

    返回启动时当天已累计的调用量，供日志确认「继续计数」而非重新计数。
    建表或读库失败只告警，不阻断服务启动。
    """
    with _LOCK:
        try:
            conn = store.connect(_cfg().db_path)
            conn.close()
        except Exception as e:
            warn(f"调用量库初始化失败: {e}")
        return _ensure_day(_today())


def _flush_date(date_str: str) -> bool:
    """把 date_str 的未落库增量写入 SQLite；成功才从 _PENDING 移除。

    _DAY_TOTAL 不受影响：它记的是完整累计，落库与否都不改变总数。
    """
    delta = _PENDING.get(date_str, 0)
    if not delta:
        return True
    if store.add_delta(_cfg().db_path, date_str, delta):
        _PENDING.pop(date_str, None)
        return True
    return False


def flush() -> dict:
    """把所有未落库增量写入 SQLite，返回仍待重试的增量（正常应为空）。幂等。"""
    with _LOCK:
        if not _PENDING:
            # 零 I/O：仅 import 未计数时，atexit 兜底不建库、不读配置
            return {}
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
        if _PENDING[today] >= _threshold():
            _flush_date(today)
        # 跨天残留（前一天不足阈值就跨天）顺带重试落库，成功即从字典移除
        for d in [d for d in _PENDING if d != today]:
            _flush_date(d)
        return _DAY_TOTAL[today]


def get_call_count(date_str: str) -> int:
    """单日调用量：本进程有活动直接用 _DAY_TOTAL，否则读库 + 未落库增量。"""
    with _LOCK:
        if date_str in _DAY_TOTAL:
            return _DAY_TOTAL[date_str]
        return store.read_day(_cfg().db_path, date_str) + _PENDING.get(date_str, 0)


def get_month_counts(month: str):
    """整月每天的调用量，无记录补 0；返回 [(YYYY-MM-DD, count), ...]。"""
    year, mon = int(month[:4]), int(month[5:7])
    days = calendar.monthrange(year, mon)[1]
    with _LOCK:
        persisted = store.read_month(_cfg().db_path, month)
        pending = dict(_PENDING)
        totals = dict(_DAY_TOTAL)
    out = []
    for d in range(1, days + 1):
        date_str = f"{month}-{d:02d}"
        if date_str in totals:
            # _DAY_TOTAL 已是完整累计，不能再叠加 pending
            count = totals[date_str]
        else:
            count = persisted.get(date_str, 0) + pending.get(date_str, 0)
        out.append((date_str, count))
    return out


def _reset_state_for_test():
    """仅自检用：清空进程内状态，模拟一次冷启动。生产代码不要调用。"""
    global _CFG, _THRESHOLD_OVERRIDE
    with _LOCK:
        _DAY_TOTAL.clear()
        _PENDING.clear()
        _CFG = None
        _THRESHOLD_OVERRIDE = None


# atexit 作为非 ASGI 运行方式的兜底；ASGI 正常关闭由 main.py 的 lifespan
# 在 yield 之后调用 flush（uvicorn 的 SIGTERM 退出路径不经过 atexit）。
# 两处都是幂等的。
atexit.register(flush)
