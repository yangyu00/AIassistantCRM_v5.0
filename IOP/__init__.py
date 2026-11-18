# -*- coding: utf-8 -*-
"""IOP 包：模块内自包含配置 + 可注入的面向对象客户端。

第 3 步：公开 API 收口到包顶层，``from IOP import IOPClient`` 即可。

* 配置全部定义在 ``config.py``，核心层不读外部文件、不 import 项目根
  ``config`` / ``src.*``、不挂日志 sink、不创建目录。
* 第 3 步起 ``import IOP`` 会连带加载 ``aiohttp``（``transport.py`` 需要），
  但 aiohttp 本身不建目录、不读配置，「零副作用」仍成立。
* 日志是构造时注入的可选参数；不注入 → ``NullLogger``，完全静默。

旧 4 个文件（``endpoints.py`` / ``http_session.py`` / ``get_token.py`` /
``get_custinfo.py``）截至第 3 步仍一行未改，原有 import 路径全部继续有效；
第 4 步才会退化成薄适配层。
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
