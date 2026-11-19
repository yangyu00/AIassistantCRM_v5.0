# -*- coding: utf-8 -*-
"""IOP 运行配置：内置默认值 → 模块级 ``.env`` → 冻结配置对象。

三个构造入口，**都只在被显式调用时工作，import 本模块零 I/O**：

===============================  ==========================================
``IOPConfig.from_env()``         ``IOPClient()`` 的默认路径：读 ``IOP/.env``
                                 + 系统环境变量；日常改配置只改这个文件
``IOPConfig.from_mapping({...})`` 调用方显式覆盖，完全不带文件（跨项目复用）
``IOPConfig.from_module_defaults()`` 纯内置默认值，不读文件（兼容旧调用 / 单测）
===============================  ==========================================

优先级：**系统环境变量 > ``IOP/.env`` > ``FIELD_SPECS`` 内置默认值**。
``.env`` 路径可用 ``IOP_ENV_FILE`` 覆盖；配置对象冻结，改完需重启进程。

⚠️ ``IOP_TOKEN_ID`` / ``IOP_TOKEN_SECRET`` 是明文凭据。``IOPConfig.__repr__``
   已屏蔽 ``secret``。公开分发本目录前，请把 ``IOP/.env`` 加入 ``.gitignore``
   或改用系统环境变量注入（见 ``.env.example``）。
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import dotenv

# 单轮刷新请求次数的硬上限：防止 .env 里配错数字导致长时间串行循环。
# 这是安全护栏，不暴露成可配置项。
MAX_ATTEMPTS_CAP = 20


# ---------------------------------------------------------------------------
# 1. 配置字段表：新增一个可改参数，只需要在这里加一行 + 写进 .env.example
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldSpec:
    """一个配置字段的声明。

    ``group`` / ``name`` 对应冻结配置对象的分组与字段名，``env`` 是 ``.env``
    里的键名，``kind`` 决定容错方式：``str`` 空值回落默认；``float`` / ``int``
    对缺键、类型错、NaN / inf、低于 ``minimum`` 一律回落默认。``cap`` 只夹上界。
    """

    group: str
    name: str
    env: str
    default: Any
    kind: str = "str"          # "str" | "float" | "int"
    minimum: float = 0.0
    cap: float | None = None


FIELD_SPECS: tuple[FieldSpec, ...] = (
    # --- 端点部署参数与凭据 ---
    FieldSpec("token", "host", "IOP_TOKEN_HOST", "134.80.220.55"),
    FieldSpec("token", "port", "IOP_TOKEN_PORT", "80"),
    FieldSpec("token", "id", "IOP_TOKEN_ID", "202408131154527921000059"),
    FieldSpec("token", "secret", "IOP_TOKEN_SECRET", "8bd777c80673af7833105f5bf4219a84"),
    FieldSpec("cust_info", "host", "IOP_CUSTINFO_HOST", "134.80.220.55"),
    FieldSpec("cust_info", "port", "IOP_CUSTINFO_PORT", "80"),
    FieldSpec("cust_info", "version", "IOP_CUSTINFO_VERSION", "1.0.1"),
    FieldSpec("cust_info", "type", "IOP_CUSTINFO_TYPE", "M"),

    # --- 超时（秒）；低于 0.01（含 0）一律回落默认，避免请求立刻超时 ---
    FieldSpec("timeout", "total", "IOP_TIMEOUT_TOTAL", 3.0, "float", 0.01),
    FieldSpec("timeout", "connect", "IOP_TIMEOUT_CONNECT", 1.0, "float", 0.01),
    FieldSpec("timeout", "sock_read", "IOP_TIMEOUT_SOCK_READ", 2.0, "float", 0.01),

    # --- 重试 / 退避 / 冷却 ---
    # max_attempts 是「单轮请求总次数」，不是「重试次数」。
    FieldSpec("retry", "max_attempts", "IOP_RETRY_MAX_ATTEMPTS",
              4, "int", 1.0, MAX_ATTEMPTS_CAP),
    FieldSpec("retry", "backoff_base", "IOP_RETRY_BACKOFF_BASE", 0.2, "float", 0.0),
    FieldSpec("retry", "backoff_factor", "IOP_RETRY_BACKOFF_FACTOR", 2.0, "float", 1.0),
    FieldSpec("retry", "backoff_max", "IOP_RETRY_BACKOFF_MAX", 2.0, "float", 0.0),
    # cooldown=0 表示一轮全失败后不冷却，每次请求都会重试。
    FieldSpec("retry", "cooldown", "IOP_RETRY_COOLDOWN", 5.0, "float", 0.0),

    # --- token 有效期换算（秒）---
    FieldSpec("token_lifetime", "default_expires_in",
              "IOP_LIFETIME_DEFAULT_EXPIRES_IN", 600.0, "float", 1.0),
    FieldSpec("token_lifetime", "refresh_buffer",
              "IOP_LIFETIME_REFRESH_BUFFER", 360.0, "float", 0.0),
    FieldSpec("token_lifetime", "min_lifetime",
              "IOP_LIFETIME_MIN_LIFETIME", 60.0, "float", 1.0),
    FieldSpec("token_lifetime", "max_expires_in",
              "IOP_LIFETIME_MAX_EXPIRES_IN", 86400.0, "float", 1.0),
)

_GROUPS = ("token", "cust_info", "timeout", "retry", "token_lifetime")

# 内置默认值：同时用作各 dataclass 的字段默认值，保证单一真源。
_DEFAULTS = {
    group: {s.name: s.default for s in FIELD_SPECS if s.group == group}
    for group in _GROUPS
}

# from_mapping() 的分组名兼容表：大小写不敏感，并认旧版的 TOKEN / CustInfo。
_GROUP_ALIASES = {
    "token": "token",
    "custinfo": "cust_info",
    "cust_info": "cust_info",
    "timeout": "timeout",
    "retry": "retry",
    "lifetime": "token_lifetime",
    "token_lifetime": "token_lifetime",
}


# ---------------------------------------------------------------------------
# 2. 读取模块级 .env
# ---------------------------------------------------------------------------
def default_env_path() -> Path:
    """默认的模块级 ``.env``：与 ``config.py`` 同目录，不依赖工作目录。"""
    return Path(__file__).with_name(".env")


def load_env(env_path=None) -> dict[str, str]:
    """按优先级取回生效的扁平键值（只取 ``FIELD_SPECS`` 白名单里的键）。

    路径：显式 ``env_path`` > 系统环境变量 ``IOP_ENV_FILE`` > ``IOP/.env``。
    同名键系统环境变量优先；文件不存在时全部回落内置默认值。
    """
    path = Path(env_path or os.environ.get("IOP_ENV_FILE") or default_env_path())
    file_values = dotenv.load(path)
    values = {}
    for spec in FIELD_SPECS:
        # os.environ 里存在但为空字符串也算「显式覆盖」，交给 _coerce 回落默认。
        if spec.env in os.environ:
            values[spec.env] = os.environ[spec.env]
        elif spec.env in file_values:
            values[spec.env] = file_values[spec.env]
    return values


# ---------------------------------------------------------------------------
# 3. 归一化与数值容错
# ---------------------------------------------------------------------------
def _normalize(overrides: Mapping[str, Any] | None) -> dict[str, dict]:
    """把 ``from_mapping()`` 的入参归一成 ``{group: {name: value}}``。

    分组名大小写不敏感，兼容旧版 ``TOKEN`` / ``CustInfo``；拼错的分组或字段
    直接报错，避免「配了却没生效」被静默忽略。
    """
    out: dict[str, dict] = {}
    for key, value in (overrides or {}).items():
        group = _GROUP_ALIASES.get(str(key).lower())
        if group is None:
            raise ValueError(
                f"未知配置分组 {key!r}；可用分组：{', '.join(_GROUPS)}")
        if not isinstance(value, Mapping):
            raise ValueError(f"配置分组 {key!r} 必须是 dict，当前为 {type(value).__name__}")
        allowed = {s.name for s in FIELD_SPECS if s.group == group}
        merged = dict(out.get(group, {}))
        for name, raw in value.items():
            if str(name).lower() not in allowed:
                raise ValueError(
                    f"分组 {group!r} 没有字段 {name!r}；可用字段：{', '.join(sorted(allowed))}")
            merged[str(name).lower()] = raw
        out[group] = merged
    return out


def _coerce(spec: FieldSpec, value: Any) -> Any:
    """按字段声明做容错：任何非法值都回落内置默认，绝不向外抛异常。"""
    if spec.kind == "str":
        text = "" if value is None else str(value)
        return text if text.strip() else str(spec.default)

    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(spec.default)
    if (number != number or number in (float("inf"), float("-inf"))
            or number < spec.minimum):
        number = float(spec.default)
    if spec.cap is not None:
        number = min(number, spec.cap)
    return int(number) if spec.kind == "int" else number


# ---------------------------------------------------------------------------
# 4. 冻结配置对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TokenEndpointCfg:
    host: str = _DEFAULTS["token"]["host"]
    port: str = _DEFAULTS["token"]["port"]
    id: str = _DEFAULTS["token"]["id"]
    secret: str = field(default=_DEFAULTS["token"]["secret"], repr=False)  # repr 屏蔽凭据


@dataclass(frozen=True)
class CustInfoEndpointCfg:
    host: str = _DEFAULTS["cust_info"]["host"]
    port: str = _DEFAULTS["cust_info"]["port"]
    version: str = _DEFAULTS["cust_info"]["version"]
    type: str = _DEFAULTS["cust_info"]["type"]


@dataclass(frozen=True)
class TimeoutCfg:
    total: float = _DEFAULTS["timeout"]["total"]
    connect: float = _DEFAULTS["timeout"]["connect"]
    sock_read: float = _DEFAULTS["timeout"]["sock_read"]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = _DEFAULTS["retry"]["max_attempts"]
    backoff_base: float = _DEFAULTS["retry"]["backoff_base"]
    backoff_factor: float = _DEFAULTS["retry"]["backoff_factor"]
    backoff_max: float = _DEFAULTS["retry"]["backoff_max"]
    cooldown: float = _DEFAULTS["retry"]["cooldown"]

    def backoff_seconds(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的退避秒数（从 1 起），指数增长并封顶。"""
        return min(self.backoff_base * (self.backoff_factor ** (attempt - 1)),
                   self.backoff_max)


@dataclass(frozen=True)
class TokenLifetime:
    default_expires_in: float = _DEFAULTS["token_lifetime"]["default_expires_in"]
    refresh_buffer: float = _DEFAULTS["token_lifetime"]["refresh_buffer"]
    min_lifetime: float = _DEFAULTS["token_lifetime"]["min_lifetime"]
    max_expires_in: float = _DEFAULTS["token_lifetime"]["max_expires_in"]


_GROUP_CLASSES = {
    "token": TokenEndpointCfg,
    "cust_info": CustInfoEndpointCfg,
    "timeout": TimeoutCfg,
    "retry": RetryPolicy,
    "token_lifetime": TokenLifetime,
}


@dataclass(frozen=True)
class IOPConfig:
    """IOP 全部运行配置的不可变聚合；``repr`` 不含 ``secret``。"""

    token: TokenEndpointCfg = field(default_factory=TokenEndpointCfg)
    cust_info: CustInfoEndpointCfg = field(default_factory=CustInfoEndpointCfg)
    timeout: TimeoutCfg = field(default_factory=TimeoutCfg)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    lifetime: TokenLifetime = field(default_factory=TokenLifetime)

    @classmethod
    def from_env(cls, env_path=None, *, overrides: Mapping[str, Any] | None = None) -> "IOPConfig":
        """默认路径：``IOP/.env`` + 系统环境变量；显式 ``overrides`` 优先级最高。"""
        flat = load_env(env_path)
        raw = _normalize(overrides)
        for spec in FIELD_SPECS:
            if spec.env in flat:
                raw.setdefault(spec.group, {}).setdefault(spec.name, flat[spec.env])
        return cls._from_raw(raw)

    @classmethod
    def from_mapping(cls, overrides: Mapping[str, Any] | None = None) -> "IOPConfig":
        """调用方显式覆盖，不读任何文件。

        分组名推荐与字段名一致（``token`` / ``cust_info`` / ``timeout`` /
        ``retry`` / ``token_lifetime``），也兼容旧版 ``TOKEN`` / ``CustInfo``。
        未给的键沿用内置默认值；非法数值回落默认，不改其它字段。
        """
        return cls._from_raw(_normalize(overrides))

    @classmethod
    def from_module_defaults(cls) -> "IOPConfig":
        """纯内置默认值，不读 ``.env``、不读环境变量（兼容旧调用 / 单测）。"""
        return cls._from_raw({})

    @classmethod
    def _from_raw(cls, raw: Mapping[str, Mapping]) -> "IOPConfig":
        """按 ``FIELD_SPECS`` 逐字段取值、容错，再装配成冻结对象。"""
        by_group: dict[str, dict] = {}
        for spec in FIELD_SPECS:
            group_values = raw.get(spec.group) or {}
            value = group_values.get(spec.name, spec.default)
            by_group.setdefault(spec.group, {})[spec.name] = _coerce(spec, value)

        return cls(**{
            attr: _GROUP_CLASSES[group](**by_group[group])
            for group, attr in (
                ("token", "token"),
                ("cust_info", "cust_info"),
                ("timeout", "timeout"),
                ("retry", "retry"),
                ("token_lifetime", "lifetime"),
            )
        })
