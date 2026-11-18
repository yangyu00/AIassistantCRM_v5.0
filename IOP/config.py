# -*- coding: utf-8 -*-
"""IOP 全部运行配置：定义在模块内，不从外部导入。

本文件是 IOP 包唯一的「配置来源」。值逐项搬迁自项目根 ``config.yaml`` 的
``IOP`` 段（第 18–55 行），以及原 ``http_session.py`` / ``get_token.py`` 里的
兜底默认值。核心层不读盘、不 import 项目根 ``config``、不 import ``src.*``、
不读环境变量、不创建任何目录。

第 0 步说明：本文件与 ``IOP/__init__.py`` 均为**新增**，旧 4 个文件本次不改，
仍按原路径读根 ``config.yaml`` 继续工作。待第 4 步旧文件退化成薄壳后，
模块内常量才成为生效中的唯一数据源。

调用方可用 ``IOPConfig.from_mapping({...})`` 显式覆盖任意分组（形状与
``config.yaml`` 的 ``IOP`` 段一致，未给的键沿用模块内常量）；但默认路径
``IOPConfig.from_module_defaults()``（即后续 ``IOPClient()`` 零参数）完全取自
下面这些常量，不经过任何外部输入。

⚠️ ``TOKEN_ID`` / ``TOKEN_SECRET`` 是生产明文凭据，从 ``config.yaml`` 原样搬迁，
   未新增暴露面。``IOPConfig.__repr__`` 已屏蔽 ``secret``。独立分发本目录前，
   应改由调用方通过 ``from_mapping`` 注入或改用环境变量，并注意仓库可见性。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 1. 端点部署参数（config.yaml: IOP.TOKEN / IOP.CustInfo，原值搬迁）
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
# 2. 超时（config.yaml: IOP.timeout；同时是 http_session._DEFAULT_TIMEOUT）
# ---------------------------------------------------------------------------
TIMEOUT_TOTAL = 3.0
TIMEOUT_CONNECT = 1.0
TIMEOUT_SOCK_READ = 2.0

# ---------------------------------------------------------------------------
# 3. 重试 / 退避 / 冷却（config.yaml: IOP.retry；同时是 get_token._DEFAULT_RETRY）
# ---------------------------------------------------------------------------
RETRY_MAX_ATTEMPTS = 4          # 单轮刷新「请求总次数」，不是「重试次数」
RETRY_BACKOFF_BASE = 0.2        # 第 1 次失败后的退避秒数
RETRY_BACKOFF_FACTOR = 2.0      # 退避倍率
RETRY_BACKOFF_MAX = 2.0         # 单次退避上限
RETRY_COOLDOWN = 5.0            # 一轮全失败后的冷却秒数
MAX_ATTEMPTS_CAP = 20           # 防止配错导致长时间串行循环

# ---------------------------------------------------------------------------
# 4. token 有效期（config.yaml: IOP.token_lifetime；同时是 get_token._DEFAULT_LIFETIME）
# ---------------------------------------------------------------------------
LIFETIME_DEFAULT_EXPIRES_IN = 600.0
LIFETIME_REFRESH_BUFFER = 360.0
LIFETIME_MIN_LIFETIME = 60.0
LIFETIME_MAX_EXPIRES_IN = 86400.0


# 形状与 config.yaml 的 IOP 段完全一致：便于逐值比对，也便于 from_mapping 覆盖。
# 下划线前缀表示私有；_merge() 每次都复制，不会就地修改这份默认表。
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
# 数值容错：与原 get_token._retry_profile / _lifetime_profile 内的 num() 逐字等价
# ---------------------------------------------------------------------------
def _num(cfg, key, default, minimum, integer=False):
    """缺键 / 类型错 / NaN / inf / 低于 minimum → 回落 default。

    与原代码一致：用 ``float()`` 转换，只捕获 ``TypeError`` / ``ValueError``；
    ``bool`` 会被 ``float()`` 接受（``True → 1.0``），此处不额外排除；
    是否允许 0 由各字段的 ``minimum`` 决定，不统一按「零非法」处理。
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

        与原 ``get_token.backoff_seconds()`` 一致，只是从读模块级 ``RETRY``
        改为读实例字段。
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
        """调用方显式覆盖。``overrides`` 形状与 ``config.yaml`` 的 ``IOP`` 段相同。

        注意：这里只接受**传进来的值**，模块自身不主动去读任何配置源，
        因此与「配置不从外部导入」的决议不冲突。
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
            # 与原 http_session.client_timeout() 一致：仅 float() 转换 + 缺键回落，
            # 不带 NaN/inf/负数校验；非法值会抛 ValueError。此为既有行为，本次不改。
            timeout=TimeoutCfg(
                total=float(tmo.get("total", TIMEOUT_TOTAL)),
                connect=float(tmo.get("connect", TIMEOUT_CONNECT)),
                sock_read=float(tmo.get("sock_read", TIMEOUT_SOCK_READ)),
            ),
            # 与原 get_token._retry_profile() 一致，含 max_attempts 上限 20。
            retry=RetryPolicy(
                max_attempts=int(min(
                    _num(rt, "max_attempts", RETRY_MAX_ATTEMPTS, 1, True),
                    MAX_ATTEMPTS_CAP)),
                backoff_base=_num(rt, "backoff_base", RETRY_BACKOFF_BASE, 0.0),
                backoff_factor=_num(rt, "backoff_factor", RETRY_BACKOFF_FACTOR, 1.0),
                backoff_max=_num(rt, "backoff_max", RETRY_BACKOFF_MAX, 0.0),
                cooldown=_num(rt, "cooldown", RETRY_COOLDOWN, 0.0),
            ),
            # 与原 get_token._lifetime_profile() 一致。
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
