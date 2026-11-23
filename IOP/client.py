"""``IOPClient``：IOP 的业务门面。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from . import endpoints
from .config import IOPConfig
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
        self._config = config if config is not None else IOPConfig.load()
        self._logger = logger if logger is not None else NullLogger()
        self._transport = (transport if transport is not None
                           else AiohttpTransport(self._config, self._logger))
        self._tokens = TokenManager(
            config=self._config,
            transport=self._transport,
            logger=self._logger,
        )

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
        """token 状态与刷新逻辑。"""
        return self._tokens

    async def start(self):
        fn = getattr(self._transport, "start", None)
        if fn is not None:
            return await fn()

    async def aclose(self):
        fn = getattr(self._transport, "aclose", None)
        if fn is not None:
            return await fn()

    async def __aenter__(self) -> "IOPClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def get_customer_labels(self, tel: str) -> dict:
        """查询客户标签画像；任何失败统一降级为 ``{}``。"""
        await self._tokens.ensure_token()

        if not self._tokens.token_valid():
            # 统一降级为空 dict：返回 None 不会触发上层的 except，而会在调用方
            # 按 dict 使用时抛 TypeError，反而让最该降级的场景失败。
            self._logger.error(
                "IOP token 不可用：刷新后仍无有效 access_token，本次标签查询降级为空标签")
            return {}

        # 请求形状（URL / headers / body）由 endpoints.custinfo_request 统一生成；
        # 上游强制的明文端点、查询串 token 与 gbk 请求头契约见 endpoints.py。
        url, headers, post_data = endpoints.custinfo_request(
            self._config, tel, self._tokens.state.access_token)

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
