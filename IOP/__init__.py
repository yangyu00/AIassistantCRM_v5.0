"""IOP 客户端包：查用户标签，任何失败统一降级为 ``{}``。"""
from .config import (
    IOPConfig,
    TokenEndpointCfg,
    CustInfoEndpointCfg,
    TimeoutCfg,
    RetryPolicy,
    TokenLifetime,
)
from .transport import AiohttpTransport, HttpResponse, NullLogger, Transport
from .token import TokenManager, TokenState
from .labels import (
    STATUS_OK, STATUS_OK_EMPTY, STATUS_TOKEN_UNAVAILABLE,
    STATUS_NETWORK_ERROR, STATUS_PARSE_ERROR, STATUS_HTTP_ERROR,
    STATUS_BAD_STRUCTURE, STATUS_BUSINESS_ERROR, STATUS_MISSING_BUSIINFO,
    is_auth_failure, parse_custinfo_response, parse_custinfo_response_ex,
)
from .config import AuthFailureCfg, BackgroundRefreshCfg
from .client import IOPClient

__all__ = [
    # 门面（其他项目 / 新代码的主入口）
    "IOPClient",
    # 配置（IOPConfig.load() 读 IOP/.env 后冻结成这些对象）
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
    "TokenManager",
    "TokenState",
    # 纯函数，便于单独验证
    "parse_custinfo_response", "parse_custinfo_response_ex",
    "is_auth_failure", "AuthFailureCfg", "BackgroundRefreshCfg",
    "STATUS_OK", "STATUS_OK_EMPTY", "STATUS_TOKEN_UNAVAILABLE",
    "STATUS_NETWORK_ERROR", "STATUS_PARSE_ERROR", "STATUS_HTTP_ERROR",
    "STATUS_BAD_STRUCTURE", "STATUS_BUSINESS_ERROR", "STATUS_MISSING_BUSIINFO",
]
