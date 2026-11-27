"""IOP 配置：唯一来源就是同目录的 ``.env``。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")

# 防止 .env 配错导致长时间串行循环；安全护栏，不开放配置。
MAX_ATTEMPTS_CAP = 20

# 全部可调参数：(env 键, 默认值, 数值下界)。字符串项下界为 None。
# 数值项缺键 / 非数字 / NaN / inf / 低于下界 → 回落默认值。
# 新增参数：这里加一行 + 写进 .env / .env.example。
_SPECS = (
    ("IOP_TOKEN_HOST", "134.80.220.55", None),
    ("IOP_TOKEN_PORT", "80", None),
    ("IOP_TOKEN_ID", "202408131154527921000059", None),
    ("IOP_TOKEN_SECRET", "8bd777c80673af7833105f5bf4219a84", None),
    ("IOP_CUSTINFO_HOST", "134.80.220.55", None),
    ("IOP_CUSTINFO_PORT", "80", None),
    ("IOP_CUSTINFO_VERSION", "1.0.1", None),
    ("IOP_CUSTINFO_TYPE", "M", None),
    ("IOP_TIMEOUT_TOTAL", 3.0, 0.01),
    ("IOP_TIMEOUT_CONNECT", 1.0, 0.01),
    ("IOP_TIMEOUT_SOCK_READ", 2.0, 0.01),
    # max_attempts 是「单轮请求总次数」，不是「重试次数」。
    ("IOP_RETRY_MAX_ATTEMPTS", 4, 1),
    ("IOP_RETRY_BACKOFF_BASE", 0.2, 0.0),
    ("IOP_RETRY_BACKOFF_FACTOR", 2.0, 1.0),
    ("IOP_RETRY_BACKOFF_MAX", 2.0, 0.0),
    # cooldown=0 表示一轮全失败后不冷却。
    ("IOP_RETRY_COOLDOWN", 5.0, 0.0),
    ("IOP_LIFETIME_DEFAULT_EXPIRES_IN", 600.0, 1.0),
    ("IOP_LIFETIME_REFRESH_BUFFER", 360.0, 0.0),
    ("IOP_LIFETIME_MIN_LIFETIME", 60.0, 1.0),
    # 上游实际 expires_in 可能是 600000 秒量级（约 7 天）；上限提到 7 天，
    # 避免把长有效期硬截断到 24h 后「每天刷新一次」并且掩盖上游真实语义。
    ("IOP_LIFETIME_MAX_EXPIRES_IN", 604800.0, 1.0),
    # token 响应里有效期字段的候选名（按序取第一个存在的）。
    # 上游字段名不是标准 expires_in 时不再静默回落到 default=600s。
    ("IOP_EXPIRES_IN_FIELDS", "expires_in,expiresIn,expire_in", None),
    # 标签查询返回这些业务码 / 描述命中这些关键词时，判定为「token 失效」。
    ("IOP_AUTH_FAILURE_CODES", "401,403,1001,1002,10001,10102,30001", None),
    ("IOP_AUTH_FAILURE_KEYWORDS",
     "access_token,accesstoken,token,invalid,expired,unauthorized,unauthenticated,"
     "not valid,认证,令牌,失效,无效,过期,未授权,未登录", None),
    # 判定为 token 失效时，是否 invalidate + 重刷 + 重试一次标签查询。
    ("IOP_AUTH_RETRY_ENABLED", 1, 0),
    # 后台 token 续期：把刷新移出用户请求的关键路径。
    ("IOP_BACKGROUND_REFRESH_ENABLED", 1, 0),
    ("IOP_BACKGROUND_REFRESH_LEAD", 60.0, 1.0),
    ("IOP_BACKGROUND_REFRESH_MIN_INTERVAL", 30.0, 1.0),
    # lifespan 启动预热的最长等待，超时不阻塞服务启动。
    ("IOP_WARMUP_TIMEOUT", 5.0, 0.1),
)


def _read_env(env_path) -> dict[str, str]:
    """读 ``.env``：只认整行 ``#`` 注释与 ``KEY=VALUE``，文件不存在返回 ``{}``。"""
    path = Path(env_path)
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key:
            values[key] = value
    return values


def _resolve(values: dict[str, str]) -> dict[str, object]:
    """按 ``_SPECS`` 取值并做数值容错，返回扁平的生效值表。"""
    out = {}
    for key, default, minimum in _SPECS:
        raw = values.get(key)
        if minimum is None:                      # 字符串项
            text = (raw or "").strip()
            # 空值或显式写成 <...> 的占位符都算「没有凭据」：直接用代码里的
            # 内置实际值，不把占位符当成真实配置。
            if not text or (text.startswith("<") and text.endswith(">")):
                text = default
            out[key] = text
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            number = float(default)
        if not math.isfinite(number) or number < minimum:
            number = float(default)
        if key == "IOP_RETRY_MAX_ATTEMPTS":
            number = min(number, MAX_ATTEMPTS_CAP)
        out[key] = int(number) if isinstance(default, int) else number
    return out


def _csv(raw) -> tuple[str, ...]:
    """逗号分隔字符串 → 去空保序元组。"""
    return tuple(part.strip() for part in str(raw or "").split(",")
                 if part.strip())


# 冻结配置对象（字段无默认值：配置一律由 load() 产生）
@dataclass(frozen=True)
class TokenEndpointCfg:
    host: str
    port: str
    id: str
    secret: str = field(repr=False)              # repr 屏蔽凭据


@dataclass(frozen=True)
class CustInfoEndpointCfg:
    host: str
    port: str
    version: str
    type: str


@dataclass(frozen=True)
class TimeoutCfg:
    total: float
    connect: float
    sock_read: float


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_base: float
    backoff_factor: float
    backoff_max: float
    cooldown: float

    def backoff_seconds(self, attempt: int) -> float:
        """第 attempt 次失败后的退避秒数（从 1 起），指数增长并封顶。"""
        return min(self.backoff_base * (self.backoff_factor ** (attempt - 1)),
                   self.backoff_max)


@dataclass(frozen=True)
class TokenLifetime:
    default_expires_in: float
    refresh_buffer: float
    min_lifetime: float
    max_expires_in: float


@dataclass(frozen=True)
class AuthFailureCfg:
    """标签查询失败中「token 失效」的识别口径与是否自动重刷。"""

    codes: tuple[str, ...]
    keywords: tuple[str, ...]
    retry_enabled: bool


@dataclass(frozen=True)
class BackgroundRefreshCfg:
    """后台续期与启动预热参数。"""

    enabled: bool
    lead: float
    min_interval: float
    warmup_timeout: float


@dataclass(frozen=True)
class IOPConfig:
    """IOP 全部运行配置的不可变聚合。"""

    token: TokenEndpointCfg
    cust_info: CustInfoEndpointCfg
    timeout: TimeoutCfg
    retry: RetryPolicy
    lifetime: TokenLifetime
    expires_in_fields: tuple[str, ...]
    auth_failure: AuthFailureCfg
    background_refresh: BackgroundRefreshCfg

    @classmethod
    def load(cls, env_path=ENV_PATH) -> "IOPConfig":
        """读 ``.env`` 并冻结成配置对象。"""
        v = _resolve(_read_env(env_path))
        return cls(
            token=TokenEndpointCfg(v["IOP_TOKEN_HOST"], v["IOP_TOKEN_PORT"],
                                   v["IOP_TOKEN_ID"], v["IOP_TOKEN_SECRET"]),
            cust_info=CustInfoEndpointCfg(v["IOP_CUSTINFO_HOST"], v["IOP_CUSTINFO_PORT"],
                                         v["IOP_CUSTINFO_VERSION"], v["IOP_CUSTINFO_TYPE"]),
            timeout=TimeoutCfg(v["IOP_TIMEOUT_TOTAL"], v["IOP_TIMEOUT_CONNECT"],
                               v["IOP_TIMEOUT_SOCK_READ"]),
            retry=RetryPolicy(v["IOP_RETRY_MAX_ATTEMPTS"], v["IOP_RETRY_BACKOFF_BASE"],
                              v["IOP_RETRY_BACKOFF_FACTOR"], v["IOP_RETRY_BACKOFF_MAX"],
                              v["IOP_RETRY_COOLDOWN"]),
            lifetime=TokenLifetime(v["IOP_LIFETIME_DEFAULT_EXPIRES_IN"],
                                   v["IOP_LIFETIME_REFRESH_BUFFER"],
                                   v["IOP_LIFETIME_MIN_LIFETIME"],
                                   v["IOP_LIFETIME_MAX_EXPIRES_IN"]),
            expires_in_fields=(_csv(v["IOP_EXPIRES_IN_FIELDS"])
                               or ("expires_in",)),
            auth_failure=AuthFailureCfg(
                codes=_csv(v["IOP_AUTH_FAILURE_CODES"]),
                keywords=tuple(k.lower() for k in
                               _csv(v["IOP_AUTH_FAILURE_KEYWORDS"])),
                retry_enabled=bool(v["IOP_AUTH_RETRY_ENABLED"]),
            ),
            background_refresh=BackgroundRefreshCfg(
                enabled=bool(v["IOP_BACKGROUND_REFRESH_ENABLED"]),
                lead=v["IOP_BACKGROUND_REFRESH_LEAD"],
                min_interval=v["IOP_BACKGROUND_REFRESH_MIN_INTERVAL"],
                warmup_timeout=v["IOP_WARMUP_TIMEOUT"],
            ),
        )
