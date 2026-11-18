# -*- coding: utf-8 -*-
"""IOP 客户端包。

公开入口是 ``IOPClient``，零参数即可使用：

    from IOP import IOPClient

    async with IOPClient() as client:
        labels = await client.get_customer_labels("13900000000")

配置全部定义在本包的 ``config.py``，不读外部文件、不依赖项目其它模块。
日志由构造时注入，未注入则完全静默。

模块划分：
    config.py          运行参数与容错校验
    endpoints.py       上游请求契约（URL / 参数 / 请求头）
    endpoints_spec.py  把配置适配成请求参数
    transport.py       HTTP 会话与响应收口
    token.py           token 刷新、单飞、重试与冷却
    labels.py          标签响应校验与解析
    client.py          业务门面
"""
from .config import (
    IOPConfig,
    TokenEndpointCfg,
    CustInfoEndpointCfg,
    TimeoutCfg,
    RetryPolicy,
    TokenLifetime,
)
from .transport import AiohttpTransport, HttpResponse, NullLogger, Transport
from .endpoints_spec import EndpointSpec
from .token import TokenManager, TokenState
from .labels import parse_custinfo_response
from .client import IOPClient

__all__ = [
    # 门面（其他项目 / 新代码的主入口）
    "IOPClient",
    # 配置（全部来自模块内常量）
    "IOPConfig",
    "TokenEndpointCfg",
    "CustInfoEndpointCfg",
    "TimeoutCfg",
    "RetryPolicy",
    "TokenLifetime",
    # 可注入组件
    "Transport",
    "AiohttpTransport",
    "HttpResponse",
    "NullLogger",
    "EndpointSpec",
    "TokenManager",
    "TokenState",
    # 纯函数，便于单独验证
    "parse_custinfo_response",
]
