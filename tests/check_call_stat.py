"""调用量统计 GET 接口冒烟：用 requests 查单日 / 整月 / 非法参数并打印返回。"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from urllib.parse import urlencode

import requests

BASE_URL = os.environ.get("CRM_SMOKE_BASE_URL", "http://127.0.0.1:8990").rstrip("/")
# 与 call_stat/.env 的 CALL_STAT_QUERY_PATH 保持一致；改了 .env 记得同步这里
CALL_STAT_PATH = "/api/callStat"
TIMEOUT = 10


def get(params: dict, label: str, expect: str) -> int:
    """发一次 GET，打印 URL、耗时、完整响应。"""
    url = BASE_URL + CALL_STAT_PATH
    query = urlencode(params)
    print(f"\n--- {label} ---")
    print("  GET " + url + ("?" + query if query else ""))
    print("  期望：" + expect)

    start = time.time()
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
        print(f"  请求失败（{type(e).__name__}）：{e}")
        print("  检查服务是否已启动，或 CRM_SMOKE_BASE_URL / CALL_STAT_QUERY_PATH 是否一致。")
        return 1
    elapsed = time.time() - start

    print(f"  HTTP {resp.status_code} | 耗时 {elapsed:.3f}s | 实际 URL：{resp.url}")
    try:
        body = resp.json()
    except ValueError:
        print("  响应不是 JSON：" + resp.text[:200])
        return 1
    print("  响应体：")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # 默认查今天；给一个 YYYY-MM-DD 就查那天，月份取同月
    if argv:
        try:
            day = dt.date.fromisoformat(argv[0])
        except ValueError:
            print(f"日期格式不对：{argv[0]!r}，要 YYYY-MM-DD（例如 2026-11-16）")
            return 2
    else:
        day = dt.date.today()

    date_str = day.isoformat()
    month_str = day.strftime("%Y-%m")
    print(f"目标服务：{BASE_URL} | 统计日期：{date_str} | 统计月份：{month_str}")

    # 1) 单日：读到的是完整累计（含尚未落库的内存增量）
    rc = get({"date": date_str}, "单日查询",
             f"res_code=0，result.date={date_str}，callCount 为当天累计整数")
    if rc:
        return rc

    # 2) 整月：按 calendar.monthrange 补齐整月每一天，无记录补 0
    rc = get({"month": month_str}, "整月查询",
             f"res_code=0，result.days 覆盖 {month_str} 全月，无记录的日子补 0")
    if rc:
        return rc

    # 3) 连查两次单日：演示「GET 自身也计数，但不含本次」的规则
    rc = get({"date": date_str}, "单日查询（第二次）",
             "callCount 比第 1 次大；中间还有一次整月 GET 也计了数，通常 +2")
    if rc:
        return rc

    # 4) 四组非法参数：date/month 必须且只能提供一个，且格式严格
    invalid_cases = [
        ("两个参数都不传", {}, "res_code=1，提示必须提供其中一个参数"),
        ("同时传 date 和 month", {"date": date_str, "month": month_str},
         "res_code=1，提示两个参数只能给一个"),
        ("日期格式不补零", {"date": "2026-1-6"}, "res_code=1，日期非法"),
        ("月份越界", {"month": "2026-13"}, "res_code=1，月份非法"),
    ]
    for label, params, expect in invalid_cases:
        rc = get(params, "非法参数：" + label, expect)
        if rc:
            return rc

    print("\n全部用例已跑完（仅打印，不做断言）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
