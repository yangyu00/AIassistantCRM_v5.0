# -*- coding: utf-8 -*-
"""IOP 标签直连冒烟：给一个号码，打印查到的客户标签画像。"""
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
    """发起一次真实查询并返回客户标签画像 dict。"""
    from IOP import IOPClient

    async with IOPClient() as client:
        return await client.get_customer_labels(tel)


def _render(tel, profile):
    """把画像打印到终端。"""
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
