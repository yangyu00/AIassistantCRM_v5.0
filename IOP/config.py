# -*- coding: utf-8 -*-
"""IOP 运行配置。

本模块是 IOP 唯一的配置来源：全部参数以模块级常量直接定义，不读配置文件、
不读环境变量、不依赖项目里的其它模块。改地址、凭据或策略就改这里；需要在
运行时换一套参数时，用 ``IOPConfig.from_mapping({...})`` 显式覆盖。

``IOPClient()`` 零参数构造即使用 ``IOPConfig.from_module_defaults()``，
取值完全来自下面的常量，不经过任何外部输入。

⚠️ ``TOKEN_ID`` / ``TOKEN_SECRET`` 是生产明文凭据。``IOPConfig.__repr__``
   已屏蔽 ``secret``。分发本目录前应改由调用方通过 ``from_mapping`` 注入，
   并注意仓库可见性。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 1. 端点部署参数
# ---------------------------------------------------------------------------
TOKEN_HOST = "134.80.220.55"
TOKEN_PORT = "80"
TOKEN_ID = "202408131154527921000059"
TOKEN_SECRET = "8bd777c80673af7833105f5bf4219a84"

CUSTINFO_HOST = "134.80.220.55"
CUSTINFO_PORT = "80"
CUSTINFO_VERSION = "1.0.1"
CUSTINFO_TYPE = "M"

# ---------------------------------------------------------------------------
# 2. 超时（秒）
# ---------------------------------------------------------------------------
TIMEOUT_TOTAL = 3.0
TIMEOUT_CONNECT = 1.0
TIMEOUT_SOCK_READ = 2.0

# ---------------------------------------------------------------------------
# 3. 重试 / 退避 / 冷却
# ---------------------------------------------------------------------------
RETRY_MAX_ATTEMPTS = 4          # 单轮刷新「请求总次数」，不是「重试次数」
RETRY_BACKOFF_BASE = 0.2        # 第 1 次失败后的退避秒数
RETRY_BACKOFF_FACTOR = 2.0      # 退避倍率
RETRY_BACKOFF_MAX = 2.0         # 单次退避上限
RETRY_COOLDOWN = 5.0            # 一轮全失败后的冷却秒数
MAX_ATTEMPTS_CAP = 20           # 防止配错导致长时间串行循环

# ---------------------------------------------------------------------------
# 4. token 有效期（秒）
# ---------------------------------------------------------------------------
LIFETIME_DEFAULT_EXPIRES_IN = 600.0
LIFETIME_REFRESH_BUFFER = 360.0
LIFETIME_MIN_LIFETIME = 60.0
LIFETIME_MAX_EXPIRES_IN = 86400.0


# 默认参数表；形状与 from_mapping() 的入参一致，未给的键沿用这里的值。
# _merge() 每次都复制，不会就地修改这份默认表。
_DEFAULTS_MAPPING: dict[str, Any] = {
    "TOKEN": {
        "host": TOKEN_HOST, "port": TOKEN_PORT,
        "id": TOKEN_ID, "secret": TOKEN_SECRET,
    },
    "CustInfo": {
        "host": CUSTINFO_HOST, "port": CUSTINFO_PORT,
        "version": CUSTINFO_VERSION, "type": CUSTINFO_TYPE,
    },
    "timeout": {
        "total": TIMEOUT_TOTAL, "connect": TIMEOUT_CONNECT,
        "sock_read": TIMEOUT_SOCK_READ,
    },
    "retry": {
        "max_attempts": RETRY_MAX_ATTEMPTS, "backoff_base": RETRY_BACKOFF_BASE,
        "backoff_factor": RETRY_BACKOFF_FACTOR, "backoff_max": RETRY_BACKOFF_MAX,
        "cooldown": RETRY_COOLDOWN,
    },
    "token_lifetime": {
        "default_expires_in": LIFETIME_DEFAULT_EXPIRES_IN,
        "refresh_buffer": LIFETIME_REFRESH_BUFFER,
        "min_lifetime": LIFETIME_MIN_LIFETIME,
        "max_expires_in": LIFETIME_MAX_EXPIRES_IN,
    },
}


# ---------------------------------------------------------------------------
# 数值容错：缺键 / 类型错 / NaN / inf / 越界一律回落默认值
# ---------------------------------------------------------------------------
def _num(cfg, key, default, minimum, integer=False):
    """缺键 / 类型错 / NaN / inf / 低于 minimum → 回落 default。

    用 ``float()`` 转换，只捕获 ``TypeError`` / ``ValueError``；``bool``
    会被 ``float()`` 接受（``True → 1.0``），此处不额外排除；是否允许 0
    由各字段的 ``minimum`` 决定。
    """
    try:
        v = float(cfg.get(key, default))
    except (TypeError, ValueError):
        v = float(default)
    if v != v or v in (float('inf'), float('-inf')) or v < minimum:
        v = float(default)
    return int(v) if integer else v


def _merge(base, overrides):
    """按分组递归合并，未提供的分组 / 键沿用模块内常量。返回新 dict。"""
    out = dict(base)
    for k, v in overrides.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# 冻结配置对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TokenEndpointCfg:
    host: str = TOKEN_HOST
    port: str = TOKEN_PORT
    id: str = TOKEN_ID
    secret: str = field(default=TOKEN_SECRET, repr=False)  # repr 屏蔽凭据


@dataclass(frozen=True)
class CustInfoEndpointCfg:
    host: str = CUSTINFO_HOST
    port: str = CUSTINFO_PORT
    version: str = CUSTINFO_VERSION
    type: str = CUSTINFO_TYPE


@dataclass(frozen=True)
class TimeoutCfg:
    total: float = TIMEOUT_TOTAL
    connect: float = TIMEOUT_CONNECT
    sock_read: float = TIMEOUT_SOCK_READ


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = RETRY_MAX_ATTEMPTS
    backoff_base: float = RETRY_BACKOFF_BASE
    backoff_factor: float = RETRY_BACKOFF_FACTOR
    backoff_max: float = RETRY_BACKOFF_MAX
    cooldown: float = RETRY_COOLDOWN

    def backoff_seconds(self, attempt: int) -> float:
        """第 attempt 次失败后的退避秒数（从 1 起），指数增长并封顶。

        指数增长并封顶 ``backoff_max``。
        """
        return min(self.backoff_base * (self.backoff_factor ** (attempt - 1)),
                   self.backoff_max)


@dataclass(frozen=True)
class TokenLifetime:
    default_expires_in: float = LIFETIME_DEFAULT_EXPIRES_IN
    refresh_buffer: float = LIFETIME_REFRESH_BUFFER
    min_lifetime: float = LIFETIME_MIN_LIFETIME
    max_expires_in: float = LIFETIME_MAX_EXPIRES_IN


@dataclass(frozen=True)
class IOPConfig:
    """IOP 全部运行配置的不可变聚合。

    - ``from_module_defaults()``：完全取自本模块常量，零外部依赖。
    - ``from_mapping(overrides)``：调用方显式覆盖，未给的键沿用模块内常量。

    两者走同一条解析路径，因此容错规则、默认值完全一致。
    """

    token: TokenEndpointCfg = field(default_factory=TokenEndpointCfg)
    cust_info: CustInfoEndpointCfg = field(default_factory=CustInfoEndpointCfg)
    timeout: TimeoutCfg = field(default_factory=TimeoutCfg)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    lifetime: TokenLifetime = field(default_factory=TokenLifetime)

    @classmethod
    def from_module_defaults(cls) -> "IOPConfig":
        """零参数默认路径：不读任何外部文件 / 环境变量。"""
        return cls.from_mapping(None)

    @classmethod
    def from_mapping(cls, overrides: Mapping[str, Any] | None = None) -> "IOPConfig":
        """调用方显式覆盖。``overrides`` 形状与 ``_DEFAULTS_MAPPING`` 相同。

        未给的键沿用默认值；模块自身不主动读取任何配置源。
        """
        raw = _merge(_DEFAULTS_MAPPING, overrides or {})
        return cls._from_raw(raw)

    @classmethod
    def _from_raw(cls, raw):
        tok = raw.get("TOKEN", {}) or {}
        ci = raw.get("CustInfo", {}) or {}
        tmo = raw.get("timeout", {}) or {}
        rt = raw.get("retry", {}) or {}
        lt = raw.get("token_lifetime", {}) or {}

        return cls(
            token=TokenEndpointCfg(
                host=tok.get("host", TOKEN_HOST),
                port=tok.get("port", TOKEN_PORT),
                id=tok.get("id", TOKEN_ID),
                secret=tok.get("secret", TOKEN_SECRET),
            ),
            cust_info=CustInfoEndpointCfg(
                host=ci.get("host", CUSTINFO_HOST),
                port=ci.get("port", CUSTINFO_PORT),
                version=ci.get("version", CUSTINFO_VERSION),
                type=ci.get("type", CUSTINFO_TYPE),
            ),
            # 仅 float() 转换 + 缺键回落；非法值会抛 ValueError。
            timeout=TimeoutCfg(
                total=float(tmo.get("total", TIMEOUT_TOTAL)),
                connect=float(tmo.get("connect", TIMEOUT_CONNECT)),
                sock_read=float(tmo.get("sock_read", TIMEOUT_SOCK_READ)),
            ),
            # max_attempts 上限 20，防止配错导致长时间串行循环。
            retry=RetryPolicy(
                max_attempts=int(min(
                    _num(rt, "max_attempts", RETRY_MAX_ATTEMPTS, 1, True),
                    MAX_ATTEMPTS_CAP)),
                backoff_base=_num(rt, "backoff_base", RETRY_BACKOFF_BASE, 0.0),
                backoff_factor=_num(rt, "backoff_factor", RETRY_BACKOFF_FACTOR, 1.0),
                backoff_max=_num(rt, "backoff_max", RETRY_BACKOFF_MAX, 0.0),
                cooldown=_num(rt, "cooldown", RETRY_COOLDOWN, 0.0),
            ),
            # 有效期换算参数。
            lifetime=TokenLifetime(
                default_expires_in=_num(lt, "default_expires_in",
                                        LIFETIME_DEFAULT_EXPIRES_IN, 1.0),
                refresh_buffer=_num(lt, "refresh_buffer",
                                    LIFETIME_REFRESH_BUFFER, 0.0),
                min_lifetime=_num(lt, "min_lifetime",
                                  LIFETIME_MIN_LIFETIME, 1.0),
                max_expires_in=_num(lt, "max_expires_in",
                                    LIFETIME_MAX_EXPIRES_IN, 1.0),
            ),
        )
