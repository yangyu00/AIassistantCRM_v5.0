# -*- coding: utf-8 -*-
"""单次查询指定电话号的客户画像（IOP 标签）。

跑一次、给一个电话号、把查到的客户画像打印出来。会向 IOP 配置的地址发起
真实请求，不做任何打桩。

用法（项目根目录）：

    python -m test_local.test_iop 13900000000
    python test_local/test_iop.py 13900000000

不想每次敲参数的话，把号码填进下面的 ``DEFAULT_TEL``，然后不带参数运行。

这里复用 ``IOPClient``：token 刷新、重试与冷却、有效期换算、响应校验、
超时与会话生命周期都由它负责，脚本只负责打印。配置与服务进程共用
``IOP/.env``（或同名系统环境变量），改完重启 / 重跑即可。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    # 直接以文件路径运行时 sys.path[0] 是 test_local/，补上项目根目录。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 不带参数运行时使用的号码。留空则必须命令行给（不在代码里写死任何真实号码）。
DEFAULT_TEL = ""


async def _query(tel):
    """发起一次真实查询并返回客户标签画像 dict。

    独立脚本不经过服务生命周期，用 ``async with`` 显式管理会话，保证在同一
    event loop 内创建与关闭。
    """
    from IOP import IOPClient

    async with IOPClient() as client:
        return await client.get_customer_labels(tel)


def _render(tel, profile):
    """把画像打印到终端。只打印，不写日志文件、不落盘。"""
    print(f"客户画像查询结果 | 电话号 {tel}")
    if not profile:
        print("  未查到标签画像（IOP 返回空，或本次查询已降级）。")
        print("  常见原因：token 不可用 / 上游非 200 / 超时")
        return

    print(f"  共 {len(profile)} 项标签：")
    for i, (code, pair) in enumerate(profile.items(), 1):
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
        print("用法：python -m test_local.test_iop <电话号>")
        print("  或把号码填进 test_local/test_iop.py 的 DEFAULT_TEL 后不带参数运行。")
        return 2

    profile = asyncio.run(_query(tel))
    _render(tel, profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
