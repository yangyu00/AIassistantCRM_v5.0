# -*- coding: utf-8 -*-
"""查询参数校验与响应组装：纯函数（除取数外），供 web / CLI 共用一处口径。

日期与月份用正则 + ``strptime`` 严格校验，``2026-1-6`` / ``2026-13``
一律拒绝；失败返回 ``{"res_code": 1, ...}``（HTTP 200，与项目其它接口一致）。
"""
from __future__ import annotations

import calendar
import datetime
import re

from .counter import get_call_count, get_month_counts

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTH_RE = re.compile(r"\d{4}-\d{2}")


def is_valid_date(value) -> bool:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def is_valid_month(value) -> bool:
    if not isinstance(value, str) or not _MONTH_RE.fullmatch(value):
        return False
    return 1 <= int(value[5:7]) <= 12


def validate_params(date, month):
    """返回 ``(ok, res_desc)``：两个参数必须且只能提供一个，格式严格校验。"""
    if (date is None) == (month is None):
        return False, "请且仅提供一个参数：date(YYYY-MM-DD) 或 month(YYYY-MM)"
    if date is not None and not is_valid_date(date):
        return False, "date 格式不正确，应为 YYYY-MM-DD，如 2026-11-16"
    if month is not None and not is_valid_month(month):
        return False, "month 格式不正确，应为 YYYY-MM，如 2026-11"
    return True, ""


def error_response(desc: str) -> dict:
    return {"res_code": 1, "res_desc": desc, "result": ""}


def date_result(date: str) -> dict:
    return {"date": date, "callCount": get_call_count(date)}


def month_result(month: str) -> dict:
    return {"month": month,
            "days": [{"date": d, "callCount": c}
                     for d, c in get_month_counts(month)]}


def month_day_count(month: str) -> int:
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
