# -*- coding: utf-8 -*-
"""``IOP.get_custinfo`` —— 第 4 步：薄适配层（原 91 行实现已迁出）。

原文件负责编排「确保 token → token 不可用降级 → 构造请求 → 超时/解析异常
就地收口 → 逐层校验 → 统一降级 ``{}`` → 组装标签 dict」。

现在拆成两处，逻辑一行未丢：

===========================  ================================================
原内容                        现在
===========================  ================================================
编排 / 网络异常收口            ``IOPClient.get_customer_labels``（``IOP/client.py``）
逐层校验 + BUSIINFO 组装       ``parse_custinfo_response``（``IOP/labels.py``）
loguru 日志                   构造时注入；默认 ``NullLogger`` 静默
===========================  ================================================

``main.py`` 的 ``from IOP.get_custinfo import get_custom_info_in_global`` 与
``test_local/test_iop.py`` 的同名 import 保持有效，一行未改；
返回 ``{ITEMCODE: [ITEMNAME, ITEMVALUE]}``、失败统一 ``{}`` 的行为不变。
"""
from ._compat import get_custom_info_in_global

__all__ = ["get_custom_info_in_global"]
