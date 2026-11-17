# -*- coding: utf-8 -*-
"""单次查询指定电话号的客户画像（IOP 标签）。

像以前的调试脚本一样：跑一次、给一个电话号、把查到的客户画像打印出来。
**不是单元测试**：不打桩、不做断言、不跑多个场景，会向 ``config.yaml``
里的 IOP 地址发起真实请求。

用法（项目根目录）：

    python -m test_local.test_iop 13900000000
    python test_local/test_iop.py 13900000000

不想每次敲参数的话，把号码填进下面的 ``DEFAULT_TEL``，然后不带参数运行。

与最初的复制版不同，这里不自己拼 URL / headers / post_data，也不自己算
token 有效期，而是复用生产模块 ``IOP.get_custinfo.get_custom_info_in_global()``：
A2 单飞、D1 重试/退避/冷却、D2 有效期换算、D3 响应校验、C1 超时、C2 会话
生命周期全部自动带上，生产侧改了这里不会漏。
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

    独立脚本不进 uvicorn 的 lifespan，这里显式 init/close 一次共享 session，
    既不走「临时 session 降级」告警，也保证会话在同一个 event loop 内关闭。
    """
    from IOP.get_custinfo import get_custom_info_in_global
    from IOP.http_session import close_session, init_session

    await init_session()
    try:
        return await get_custom_info_in_global(tel)
    finally:
        await close_session()


def _render(tel, profile):
    """把画像打印到终端。只打印，不写日志文件、不落盘。"""
    print(f"客户画像查询结果 | 电话号 {tel}")
    if not profile:
        print("  未查到标签画像（IOP 返回空，或本次查询已降级）。")
        print("  常见原因：token 不可用 / 上游非 200 / 超时，详见 log/ERROR_*.log")
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
