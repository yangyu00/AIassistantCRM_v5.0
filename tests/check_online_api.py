"""在线话术接口冒烟脚本：发一次真实 POST，把返回原样打印出来。"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

# 服务地址：默认与 main.py 顶部 FASTAPI_CONFIG 的 8989 端口一致。
# 部署到别的机器时用 CRM_SMOKE_BASE_URL 覆盖，不要改代码。
BASE_URL = os.environ.get("CRM_SMOKE_BASE_URL", "http://127.0.0.1:8989").rstrip("/")
SPEECH_PATH = "/api/speechQryService"   # main.py::MARKET_ASSISTANT_URL
TIMEOUT = 10                            # 秒；服务没起时别把脚本挂死

# 示例入参：占位值，不是真实客户数据。
# 换成真实号码与真实产品 ID 才能拿到业务话术；也可直接由命令行传入。
DEFAULT_TEL = "18866816199"
DEFAULT_PRODUCT_IDS = ["202502120000001"]
DEFAULT_SCENE_ID = "12345"


def call(tel: str, product_ids: list[str]) -> int:
    """发一次 POST，打印请求、耗时、完整响应与逐条话术摘要。"""
    url = BASE_URL + SPEECH_PATH
    # productId 的接口形状是 [{"id": ...}, ...]，不是字符串列表
    payload = {
        "sceneId": DEFAULT_SCENE_ID,
        "telnum": tel,
        "productId": [{"id": pid} for pid in product_ids],
    }

    print(f"POST {url}")
    print("请求体：" + json.dumps(payload, ensure_ascii=False))

    start = time.time()
    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
        # 最常见的原因是服务没起，或 BASE_URL 端口与 main.py 的 FASTAPI_CONFIG 不一致
        print(f"请求失败（{type(e).__name__}）：{e}")
        print("检查：python main.py 是否已启动；或用 CRM_SMOKE_BASE_URL 指向正确地址。")
        return 1
    elapsed = time.time() - start

    print(f"HTTP {resp.status_code} | 耗时 {elapsed:.3f}s")
    try:
        body = resp.json()
    except ValueError:
        # 服务返回 HTML（例如 404 页 / 网关错误页）时给个可读提示
        print("响应不是 JSON，原文前 500 字：")
        print(resp.text[:500])
        return 1

    print("响应体：")
    print(json.dumps(body, ensure_ascii=False, indent=2))

    # 小结：响应体可能很长，把每条话术的 code 与话术长度摘出来方便一眼扫。
    # code="0" 表示拼出了业务话术（含诊断不适用后由平级卖点话术模块给出的四段话术）；
    # code="2" 表示连维表里也没有该 product_id 时的空话术。
    scripts = (body.get("result") or {}).get("allScripts") or []
    print(f"小结：res_code={body.get('res_code')} | 共 {len(scripts)} 条话术")
    for i, one in enumerate(scripts, 1):
        print(f"  [{i}] productId={one.get('productId')} | code={one.get('code')} "
              f"| productName={one.get('productName')!r} "
              f"| script 长度={len(one.get('script') or '')}")
    return 0


def main(argv=None) -> int:
    """命令行：第一个参数是手机号，后面全部当作产品 ID。"""
    argv = sys.argv[1:] if argv is None else list(argv)
    tel = argv[0] if argv else DEFAULT_TEL
    product_ids = argv[1:] if len(argv) > 1 else DEFAULT_PRODUCT_IDS
    return call(tel, product_ids)


if __name__ == "__main__":
    raise SystemExit(main())
