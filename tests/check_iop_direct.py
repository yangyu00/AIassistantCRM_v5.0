# -*- coding: utf-8 -*-
"""IOP 标签直连冒烟：给一个号码，打印查到的客户标签画像。

前身是 ``test_local/test_iop.py``，搬进 ``tests/`` 后改名并补充注释。
它同样是**手工冒烟脚本，不是 pytest 单元测试**：跑一次、打一次真实请求、
把结果打印出来，不写 assert，文件名也不带 ``test_`` 前缀。

与 tests/ 里另外三个脚本的区别（重要）：

- ``check_online_api.py`` / ``check_param_validation.py`` / ``check_call_stat.py``
  都是用 requests 打**本机在线服务**的 HTTP 接口；
- 本脚本**不经过在线服务**，直接复用 ``IOPClient`` 打 IOP 上游。在线 API 没有
  单独的「查标签」端点，标签只存在于 marketAssistant 内部流程里，想单独验证
  上游（token、超时、标签维表）就必须直连。
- 因此它的用途是排查分层：直连能查到标签、但在线接口拿不到画像，问题在编排；
  直连就是空的，问题在 IOP 上游或 ``IOP/.env``。

会向 IOP 配置的地址发起真实请求（刷新 token、消耗上游调用），不做任何打桩。
配置与服务进程共用 ``IOP/.env``，改完重启服务 / 重跑脚本即可生效。需要
``IOPClient`` 的依赖（aiohttp 等）已装好。

用法（项目根目录下）：

    python tests/check_iop_direct.py 13900000000
    python -m tests.check_iop_direct 13900000000

不想每次敲参数的话，把号码填进下面的 ``DEFAULT_TEL``，然后不带参数运行。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    # 直接以文件路径运行时 sys.path[0] 是 tests/，补上项目根目录才能 import IOP。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 不带参数运行时使用的号码。留空则必须命令行给（不在代码里写死任何真实号码）。
DEFAULT_TEL = ""


async def _query(tel):
    """发起一次真实查询并返回客户标签画像 dict。

    独立脚本不经过服务生命周期，用 ``async with`` 显式管理会话，保证在同一
    event loop 内创建与关闭。token 刷新、重试与冷却、有效期换算、响应校验、
    超时都由 IOPClient 负责，这里只负责拿结果。
    """
    from IOP import IOPClient

    async with IOPClient() as client:
        return await client.get_customer_labels(tel)


def _render(tel, profile):
    """把画像打印到终端。只打印，不写日志文件、不落盘、不做断言。"""
    print(f"客户画像查询结果 | 电话号 {tel}")
    if not profile:
        print("  未查到标签画像（IOP 返回空，或本次查询已降级）。")
        print("  常见原因：token 不可用 / 上游非 200 / 超时 / 号码不在标签库")
        return

    print(f"  共 {len(profile)} 项标签：")
    for i, (code, pair) in enumerate(profile.items(), 1):
        # 标签值在不同上游版本里可能是 (名称, 值) 二元组，也可能就是单个值
        if isinstance(pair, (list, tuple)):
            name = pair[0] if len(pair) > 0 else ""
            value = pair[1] if len(pair) > 1 else ""
        else:
            name, value = pair, ""
        print(f"  [{i}] ITEMCODE={code} | 名称={name} | 值={value}")

    print("原始返回（dict）：")
    print(json.dumps(profile, ensure_ascii=False, indent=2))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    tel = (argv[0].strip() if argv else DEFAULT_TEL.strip())
    if not tel:
        print("用法：python tests/check_iop_direct.py <电话号>")
        print("  或把号码填进本文件的 DEFAULT_TEL 后不带参数运行。")
        return 2

    profile = asyncio.run(_query(tel))
    _render(tel, profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
