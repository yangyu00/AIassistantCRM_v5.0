# -*- coding: utf-8 -*-
"""标签响应解析：``get_custinfo.py`` 后半段（逐层校验 + BUSIINFO 组装）平移。

第 3 步新增。**纯函数**、无状态、无 I/O：logger 由调用方注入，``cost_ms``
由编排层算好传入，函数本身不 import aiohttp / 日志模块 / 配置模块，便于单独
喂任意 dict 验证。

失败分支记 ERROR 并返回 ``{}``，成功分支记 INFO 并返回标签 dict。文案、级别、
字段顺序与旧 ``get_custom_info_in_global()`` 逐字一致。

⚠️ 成功但 ``BUSIINFO`` 为空同样返回 ``{}``，与降级返回值相同——旧代码即如此，
   区分只能靠日志（成功 INFO / 失败 ERROR）。本次不「顺手修好」。
"""
from __future__ import annotations

from typing import Any


def parse_custinfo_response(
    res_data: Any,
    *,
    status_code: int,
    logger: Any,
    cost_ms: float,
) -> dict:
    """把已解析的响应体逐层校验，组装成 ``{ITEMCODE: [ITEMNAME, ITEMVALUE]}``。

    校验顺序与旧代码一致：HTTP 状态 → 响应体类型 → ``res_code`` → ``BUSIINFO``
    → 逐项组装。任一环节失败即记 ERROR 并降级为 ``{}``。
    """
    if status_code != 200:
        logger.error(f"IOP 标签查询失败 | status={status_code} | cost={cost_ms:.0f}ms")
        return {}

    if not isinstance(res_data, dict):
        logger.error(
            f"IOP 标签查询响应结构异常 | status={status_code} | "
            f"type={type(res_data).__name__} | cost={cost_ms:.0f}ms"
        )
        return {}

    # 上游可能返回字符串 "0" 或数字 0，统一按字符串比较
    res_code = str(res_data.get("res_code"))
    if res_code != "0":
        logger.error(
            f"IOP 标签查询返回失败 | status={status_code} | res_code={res_code} | "
            f"res_desc={res_data.get('res_desc', '')} | cost={cost_ms:.0f}ms"
        )
        return {}

    result = res_data.get("result")
    busi_info = result.get("BUSIINFO") if isinstance(result, dict) else None
    if not isinstance(busi_info, list):
        logger.error(
            f"IOP 标签查询缺少 BUSIINFO | status={status_code} | "
            f"type={type(busi_info).__name__} | cost={cost_ms:.0f}ms"
        )
        return {}

    custom_label_dict = {}
    for custom_label in busi_info:
        if not isinstance(custom_label, dict):
            continue
        item_code = custom_label.get("ITEMCODE")
        if item_code is None:
            continue
        custom_label_dict.update({item_code: [custom_label.get("ITEMNAME"),
                                              custom_label.get("ITEMVALUE")]})

    logger.info(
        f"IOP 标签查询成功 | status={status_code} | "
        f"busi_info={len(busi_info)} | labels={len(custom_label_dict)} | cost={cost_ms:.0f}ms"
    )
    return custom_label_dict
