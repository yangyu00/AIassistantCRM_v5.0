# -*- coding: utf-8 -*-
"""兼容层：进程级默认 ``IOPClient`` 实例 + 旧模块符号的转发。

第 4 步新增。只服务**本项目**的旧 import 路径；独立分发 ``IOP/`` 时可以不带
本文件（核心层不依赖它）。

为什么要这一层
--------------
第 0–3 步建好了可独立使用、可注入、状态隔离的 ``IOPClient``，但 ``main.py``
与 ``test_local/test_iop.py`` 仍 import 旧模块里的函数/全局。为避免动在线入口，
这里维护**一个**懒创建的默认实例，旧符号全部转发到它——没有第二套实现。

两条审核决议在这里落地
----------------------
* **配置自包含**：默认实例用 ``IOPConfig.from_module_defaults()``，不读项目根
  ``config.yaml``、不 import 根 ``config``。本文件与 3 个薄壳文件都不 import
  ``src.log_util``。
* **日志注入**：默认实例**不传 logger** → ``NullLogger`` → 完全静默。想恢复
  生产日志时，在启动代码里显式调一次 ``configure_logger(my_log)``；IOP 不替
  调用方决定。

⚠️ 默认实例仍是进程级全局，所以「实例隔离」只对显式构造 ``IOPClient`` 的
   使用方生效。兼容层本身不解决多实例隔离，也不宣称解决。
"""
from __future__ import annotations

from typing import Any

from .client import IOPClient
from .config import IOPConfig

_default: IOPClient | None = None


def default_client() -> IOPClient:
    """返回（必要时懒创建）进程级默认实例。"""
    global _default
    if _default is None:
        _default = IOPClient(config=IOPConfig.from_module_defaults())
        # 注意：不传 logger → NullLogger → 静默（决议 2）
    return _default


def configure_logger(logger: Any) -> None:
    """可选：为默认实例注入 logger，恢复 IOP 日志输出。

    默认不调用。门面、transport、TokenManager 在构造时各自持有同一个 logger
    引用，因此这里要同步替换三处，避免出现「一半静默一半记日志」。

    未注入 logger 时 IOP 侧零输出；注入后文案、级别与薄壳化前逐条一致。
    """
    c = default_client()
    c._logger = logger
    for component in (c._transport, c._tokens):
        if hasattr(component, "_logger"):
            component._logger = logger


def config_view() -> dict:
    """旧 ``http_session.global_config`` 的等价物：模块内 IOP 段的只读形状。

    形状与 ``config.yaml`` 的 ``IOP`` 段一致，便于旧自检脚本按
    ``global_config['IOP']['timeout']`` 读取；但值全部来自 ``IOP/config.py``
    的模块内常量，**不读盘**。

    ⚠️ 与旧语义的差异：旧 ``global_config`` 是整份项目配置的单例（含
       ``fastapi`` / ``marketAssistant`` / ``sftp`` 等段）；这里只有 ``IOP`` 段。
       IOP 内部与 ``main.py`` / ``test_local`` 都不依赖其它段，故不影响线上行为。
    """
    cfg = default_client().config
    return {"IOP": {
        "TOKEN": {
            "host": cfg.token.host, "port": cfg.token.port,
            "id": cfg.token.id, "secret": cfg.token.secret,
        },
        "CustInfo": {
            "host": cfg.cust_info.host, "port": cfg.cust_info.port,
            "version": cfg.cust_info.version, "type": cfg.cust_info.type,
        },
        "timeout": {
            "total": cfg.timeout.total, "connect": cfg.timeout.connect,
            "sock_read": cfg.timeout.sock_read,
        },
        "retry": {
            "max_attempts": cfg.retry.max_attempts,
            "backoff_base": cfg.retry.backoff_base,
            "backoff_factor": cfg.retry.backoff_factor,
            "backoff_max": cfg.retry.backoff_max,
            "cooldown": cfg.retry.cooldown,
        },
        "token_lifetime": {
            "default_expires_in": cfg.lifetime.default_expires_in,
            "refresh_buffer": cfg.lifetime.refresh_buffer,
            "min_lifetime": cfg.lifetime.min_lifetime,
            "max_expires_in": cfg.lifetime.max_expires_in,
        },
    }}


class _TokensProxy:
    """旧模块级 ``TOKENS`` 单例的代理：属性读写转发到默认实例 state。

    旧代码里既有 ``TOKENS.access_token`` 读取，也有
    ``TOKENS.access_token = None`` / ``TOKENS.deadline = ...`` 赋值，因此读写
    都要转发。``deadline`` 恒为 ``datetime`` 由 ``TokenState`` 保证。
    """

    def __getattr__(self, name):
        return getattr(default_client().tokens, name)

    def __setattr__(self, name, value):
        setattr(default_client().tokens, name, value)

    def __repr__(self):
        return f"<TOKENS proxy -> {default_client().tokens!r}>"


TOKENS = _TokensProxy()


def retry_dict() -> dict:
    """旧模块级 ``RETRY`` dict 的快照。

    ⚠️ 兼容差异：这是**普通 dict 快照**，供旧自检脚本读取；修改它不会改变默认
       实例的刷新策略（默认实例读的是冻结的 ``RetryPolicy``）。需要改策略请
       显式构造 ``IOPClient(IOPConfig.from_mapping({"retry": {...}}))``。
       生产代码与 ``main.py`` / ``test_local`` 都不写这个 dict。
    """
    r = default_client().config.retry
    return {
        "max_attempts": r.max_attempts,
        "backoff_base": r.backoff_base,
        "backoff_factor": r.backoff_factor,
        "backoff_max": r.backoff_max,
        "cooldown": r.cooldown,
    }


def lifetime_dict() -> dict:
    """旧模块级 ``LIFETIME`` dict 的快照（语义同 ``retry_dict()``）。"""
    p = default_client().config.lifetime
    return {
        "default_expires_in": p.default_expires_in,
        "refresh_buffer": p.refresh_buffer,
        "min_lifetime": p.min_lifetime,
        "max_expires_in": p.max_expires_in,
    }


RETRY = retry_dict()
LIFETIME = lifetime_dict()


# -- 旧函数：全部转发到默认实例 / 默认实例 transport -------------------------
async def get_custom_info_in_global(tel):
    """原 ``get_custinfo.get_custom_info_in_global``。"""
    return await default_client().get_customer_labels(tel)


async def init_session():
    """原 ``http_session.init_session``，转发到 transport.start()。"""
    return await default_client().start()


async def close_session():
    """原 ``http_session.close_session``，转发到 transport.aclose()。"""
    return await default_client().aclose()


def session_context():
    """原 ``http_session.session_context``，直接返回 transport 的上下文管理器。"""
    return default_client().transport.session_context()


def get_session():
    """原 ``http_session.get_session``。"""
    return default_client().transport.get_session()


def client_timeout():
    """原 ``http_session.client_timeout``。"""
    return default_client().transport.client_timeout()


def ensure_token():
    """原 ``get_token.ensure_token``。"""
    return default_client().token_manager.ensure_token()


def token_valid() -> bool:
    """原 ``get_token.token_valid``。"""
    return default_client().token_manager.token_valid()


def token2global():
    """原 ``get_token.token2global``（单次刷新原语）。"""
    return default_client().token_manager.token2global()


def effective_lifetime(raw):
    """原 ``get_token.effective_lifetime``。"""
    return default_client().token_manager.effective_lifetime(raw)


def backoff_seconds(attempt: int) -> float:
    """原 ``get_token.backoff_seconds``。"""
    return default_client().token_manager.backoff_seconds(attempt)
