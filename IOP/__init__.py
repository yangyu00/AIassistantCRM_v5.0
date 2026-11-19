# -*- coding: utf-8 -*-
"""IOP 客户端包：查用户标签，任何失败统一降级为 ``{}``。

公开入口是 ``IOPClient``，零参数即可使用：

    from IOP import IOPClient

    async with IOPClient() as client:
        labels = await client.get_customer_labels("13900000000")

日常改地址、凭据或策略只改 ``IOP/.env``（改完重启进程）；系统环境变量
同名键优先。跨项目复用可传 ``IOPConfig.from_mapping({...})``，完全不带文件。
日志由构造时注入，未注入则完全静默；``import IOP`` 不读任何文件。

模块层次（从上到下单向依赖，找问题时按这个顺序看）：

    dotenv.py / config.py   .env 解析、字段表、数值容错、冻结配置对象
    endpoints.py            上游请求契约（URL / 参数 / 请求头），两个纯函数
    transport.py            HTTP 会话与响应收口（唯一依赖 aiohttp 的地方）
    token.py                token 刷新、单飞、重试与冷却
    labels.py               标签响应校验与解析（纯函数）
    client.py               业务门面，编排上面各层

打桩 / 换实现只需注入 ``Transport``；请求形状不需要单独替换，由
``endpoints.py`` 按传入的 config 现算。
"""
from .config import (
    IOPConfig,
    TokenEndpointCfg,
    CustInfoEndpointCfg,
    TimeoutCfg,
    RetryPolicy,
    TokenLifetime,
    FIELD_SPECS,
    load_env,
    default_env_path,
)
from .transport import AiohttpTransport, HttpResponse, NullLogger, Transport
from .token import TokenManager, TokenState
from .labels import parse_custinfo_response
from .client import IOPClient

__all__ = [
    # 门面（其他项目 / 新代码的主入口）
    "IOPClient",
    # 配置（.env / 系统环境变量 / from_mapping，最终都冻结成这些对象）
    "IOPConfig",
    "TokenEndpointCfg",
    "CustInfoEndpointCfg",
    "TimeoutCfg",
    "RetryPolicy",
    "TokenLifetime",
    "FIELD_SPECS",
    "load_env",
    "default_env_path",
    # 可注入组件
    "Transport",
    "AiohttpTransport",
    "HttpResponse",
    "NullLogger",
    "TokenManager",
    "TokenState",
    # 纯函数，便于单独验证
    "parse_custinfo_response",
]
