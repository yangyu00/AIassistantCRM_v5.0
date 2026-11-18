# -*- coding: utf-8 -*-
"""``IOP.get_token`` —— 第 4 步：薄适配层（原 333 行实现已迁出）。

原文件负责：``TOKENS`` 单例、重试/退避/冷却参数、token 单飞（锁 + 代次）、
有效期换算、单次刷新与 7 类响应校验。

现在这些职责分别落在：

===================================  =========================================
原内容                                现在
===================================  =========================================
``RETRY`` / ``LIFETIME`` 参数解析     ``IOPConfig``（``IOP/config.py``）
``Tokens`` 单例                       ``TokenState``（``IOP/token.py``）
单飞锁 / 代次 / 冷却 / 重试循环       ``TokenManager`` 实例字段
``token2global`` 7 类响应校验          ``TokenManager.token2global``
``effective_lifetime`` 等算术          ``TokenManager`` 同名方法
loguru 日志                           构造时注入；默认 ``NullLogger`` 静默
===================================  =========================================

本文件**只做转发**，没有第二套实现；并发算法是原样平移，不是重写。
所有旧符号（含 ``client_timeout`` / ``global_config`` 的转出）保持可用。

⚠️ ``RETRY`` / ``LIFETIME`` 现在是普通 dict **快照**：读得到值，但改它不会
   影响默认实例的刷新策略；需要改策略请显式构造
   ``IOPClient(IOPConfig.from_mapping({"retry": {...}}))``。
   生产代码不写这两个 dict。
"""
from ._compat import (
    LIFETIME,
    RETRY,
    TOKENS,
    backoff_seconds,
    client_timeout,
    config_view,
    effective_lifetime,
    ensure_token,
    token2global,
    token_valid,
)

# 原文件通过 `from IOP.http_session import ...` 带出这两个名字，保留转出以免
# 破坏 `from IOP.get_token import client_timeout / global_config` 这类旧 import。
global_config = config_view()

__all__ = [
    "TOKENS",
    "RETRY",
    "LIFETIME",
    "ensure_token",
    "token_valid",
    "token2global",
    "effective_lifetime",
    "backoff_seconds",
    "client_timeout",
    "global_config",
]
