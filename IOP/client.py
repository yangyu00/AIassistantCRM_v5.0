# -*- coding: utf-8 -*-
"""``IOPClient``：IOP 的面向对象门面。

第 3 步新增。``get_custinfo.py`` 的编排逻辑（确保 token → 不可用降级 → 构造
请求 → 超时/解析异常就地收口 → 逐层校验 → ``{}`` 降级）**原样平移**到这里，
只把模块级函数/全局换成实例方法/字段，把 ``my_log`` 换成注入的 logger。

零参数可用：``async with IOPClient() as client:``
- ``config=None``  → ``IOPConfig.from_module_defaults()``（模块内常量，零外部依赖）
- ``transport=None`` → 自建 ``AiohttpTransport``
- ``logger=None`` → ``NullLogger``，未注入则不记录日志

``TokenManager`` 与门面**显式共享同一个** transport / spec / logger，不会各自
再建第二套会话；因此一次 ``start()`` / ``aclose()` 覆盖 token 与标签两端点。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from .config import IOPConfig
from .endpoints_spec import EndpointSpec
from .labels import parse_custinfo_response
from .token import TokenManager
from .transport import AiohttpTransport, NullLogger, Transport


class IOPClient:
    """一次 IOP 会话的门面：``get_customer_labels(tel)`` 是唯一业务入口。"""

    def __init__(
        self,
        config: IOPConfig | None = None,
        transport: Transport | None = None,
        logger: Any = None,
    ) -> None:
        self._config = config if config is not None else IOPConfig.from_module_defaults()
        self._logger = logger if logger is not None else NullLogger()
        self._transport = (transport if transport is not None
                           else AiohttpTransport(self._config, self._logger))
        self._spec = EndpointSpec(self._config)
        self._tokens = TokenManager(
            config=self._config,
            transport=self._transport,
            spec=self._spec,
            logger=self._logger,
        )

    # -- 只读属性（第 4 步兼容层 / 自检用） ----------------------------------
    @property
    def config(self) -> IOPConfig:
        return self._config

    @property
    def logger(self):
        return self._logger

    @property
    def transport(self):
        return self._transport

    @property
    def token_manager(self) -> TokenManager:
        return self._tokens

    @property
    def tokens(self):
        """原模块级 ``TOKENS`` 的等价物（``TokenState`` 实例）。"""
        return self._tokens.state

    # -- 生命周期 ------------------------------------------------------------
    async def start(self):
        """转发到 transport.start()。注入的假传输层没有该方法时静默跳过。"""
        fn = getattr(self._transport, "start", None)
        if fn is not None:
            return await fn()

    async def aclose(self):
        """转发到 transport.aclose()。注入的假传输层没有该方法时静默跳过。"""
        fn = getattr(self._transport, "aclose", None)
        if fn is not None:
            return await fn()

    async def __aenter__(self) -> "IOPClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # -- 业务入口（原 get_custom_info_in_global 逐字平移） --------------------
    async def get_customer_labels(self, tel: str) -> dict:
        """查询客户标签画像；任何失败统一降级为 ``{}``，绝不外抛。

        返回 ``{ITEMCODE: [ITEMNAME, ITEMVALUE]}``。与旧函数一致：成功但 0 标签
        也返回 ``{}``，区分靠注入的 logger（成功 INFO / 失败 ERROR）。
        """
        await self._tokens.ensure_token()

        if not self._tokens.token_valid():
            # 统一降级为空 dict：返回 None 不会触发 main.py 的 except，会一路传到
            # ProductInfoRaw 并抛 TypeError，反而让最该降级的场景返回 500。
            self._logger.error(
                "IOP token 不可用：刷新后仍无有效 access_token，本次标签查询降级为空标签")
            return {}

        # 上游接口约定：access_token 必须放在 URL 查询串中（不接受 Authorization 头），
        # 且只提供 http 明文端点。均为上游实际要求，不可改动。
        # 上游强制要求 Content-Type 声明 charset=gbk 并显式携带 Accept-Encoding，
        # 即使与实际序列化编码不一致也需照发，不可改动。
        url, headers, post_data = self._spec.custinfo_request(
            tel, self._tokens.state.access_token)

        start = time.perf_counter()
        # 复用应用级共享 session（lifespan 创建/关闭）。
        # session 已带 ClientTimeout；连接/读超时与网络异常就地收口，
        # 不让异常穿透到上层（那样 IOP 侧会没有日志）。
        try:
            resp = await self._transport.post(url, json=post_data, headers=headers)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            cost_ms = (time.perf_counter() - start) * 1000
            self._logger.error(
                f"IOP 标签查询网络失败 | error={type(e).__name__}: {e} | cost={cost_ms:.0f}ms"
            )
            return {}

        status_code = resp.status
        try:
            # 上游返回的 Content-Type 不规范，不做类型校验
            res_data = await resp.json()
        except Exception as e:
            self._logger.error(
                f"IOP 标签查询响应解析失败 | status={status_code} | error={e}")
            return {}

        cost_ms = (time.perf_counter() - start) * 1000
        return parse_custinfo_response(
            res_data, status_code=status_code,
            logger=self._logger, cost_ms=cost_ms)
