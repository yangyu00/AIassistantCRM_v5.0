# -*- coding: utf-8 -*-
"""入参校验冒烟：用 requests 连打几组非法请求，打印每次的实际返回。"""
from __future__ import annotations

import json
import os
import time

import requests

BASE_URL = os.environ.get("CRM_SMOKE_BASE_URL", "http://127.0.0.1:8989").rstrip("/")
SPEECH_PATH = "/api/speechQryService"
TIMEOUT = 10

# 正常对照组用的占位值；换成真实数据可顺带看正常响应
TEL = "18866816199"
PRODUCT_ID = "202502120000001"


def _payload(telnum, product_ids):
    """组装请求体。"""
    body = {"sceneId": "12345", "telnum": telnum}
    if product_ids is not None:
        body["productId"] = product_ids
    return body


# (用例名, 请求体, 期望的中文说明)。期望只是打印给人对照，不做断言。
CASES = [
    ("正常请求（对照组）",
     _payload(TEL, [{"id": PRODUCT_ID}]),
     "res_code=0，allScripts 非空"),

    ("手机号为空串",
     _payload("", [{"id": PRODUCT_ID}]),
     "res_code=1，电话号输入不正确"),

    ("手机号含非数字",
     _payload("1886681619a", [{"id": PRODUCT_ID}]),
     "res_code=1，电话号输入不正确"),

    ("手机号位数不足（10 位）",
     _payload("1886681619", [{"id": PRODUCT_ID}]),
     "res_code=1，电话号输入不正确"),

    ("productId 为空列表",
     _payload(TEL, []),
     "res_code=1，产品ID输入不正确"),

    ("productId 里某个 id 为空串",
     _payload(TEL, [{"id": ""}]),
     "res_code=1，产品ID输入不正确"),

    # 注意：ProductIds.id 与 Args.productId 都有默认值（"" / []），
    # 所以下面两组都是先被字段校验器拦成 productIdError，而不是
    # RequestValidationError；真正走 RequestValidationError 的是类型不对（最后一组）。
    ("productId 元素缺 id 字段",
     _payload(TEL, [{"foo": PRODUCT_ID}]),
     "res_code=1，产品ID输入不正确（id 落到默认空串）"),

    ("整个 productId 字段缺失",
     _payload(TEL, None),
     "res_code=1，产品ID输入不正确（字段默认值是空列表）"),

    ("productId 传字符串而不是列表",
     _payload(TEL, PRODUCT_ID),
     "res_code=1，参数校验错误（RequestValidationError）"),
]


def run() -> int:
    url = BASE_URL + SPEECH_PATH
    print(f"目标：{url} | 共 {len(CASES)} 组用例（每组都会让调用量 +1）")

    for i, (name, payload, expect) in enumerate(CASES, 1):
        print(f"\n--- [{i}/{len(CASES)}] {name} ---")
        print("  请求体：" + json.dumps(payload, ensure_ascii=False))
        print("  期望：" + expect)

        start = time.time()
        try:
            resp = requests.post(url, json=payload, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            print(f"  请求失败（{type(e).__name__}）：{e}")
            print("  检查服务是否已启动，或 CRM_SMOKE_BASE_URL 是否指向正确地址。")
            return 1
        elapsed = time.time() - start

        try:
            body = resp.json()
        except ValueError:
            print(f"  HTTP {resp.status_code} | 响应不是 JSON：{resp.text[:200]}")
            continue

        # 只摘关键字段，避免八组完整响应刷屏；想看全量可临时改成 dumps 整个 body
        print(f"  HTTP {resp.status_code} | 耗时 {elapsed:.3f}s")
        print(f"  实际：res_code={body.get('res_code')} | res_desc={body.get('res_desc')!r} "
              f"| result={json.dumps(body.get('result'), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
