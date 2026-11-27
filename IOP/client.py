"""``IOPClient``：IOP 的业务门面。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from . import endpoints
from .config import IOPConfig
from .labels import (
    STATUS_NETWORK_ERROR, STATUS_PARSE_ERROR, STATUS_TOKEN_UNAVAILABLE,
    parse_custinfo_response_ex,
)
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
        # 线上 main.py 必须注入 logger；默认 NullLogger 会把所有失败/冷却原因
        # 全部吞掉，导致「为什么这次没有辅助画像」完全无法排查。
        self._logger = logger if logger is not None else NullLogger()
        self._transport = (transport if transport is not None
                           else AiohttpTransport(self._config, self._logger))
        self._tokens = TokenManager(
            config=self._config,
            transport=self._transport,
            logger=self._logger,
        )
        self._background_task = None

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

    # ------------------------------------------------------------------
    # 生命周期：启动预热 + 后台续期，把刷新移出用户请求关键路径
    # ------------------------------------------------------------------
    async def start(self):
        fn = getattr(self._transport, "start", None)
        if fn is not None:
            await fn()
        await self.warmup()
        self._start_background_refresh()

    async def aclose(self):
        await self._stop_background_refresh()
        fn = getattr(self._transport, "aclose", None)
        if fn is not None:
            return await fn()

    async def __aenter__(self) -> "IOPClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def warmup(self):
        """启动时尽力预热 token，失败 / 超时都不阻塞服务启动。"""
        timeout = self._config.background_refresh.warmup_timeout
        try:
            await asyncio.wait_for(self._tokens.ensure_token(), timeout=timeout)
        except asyncio.TimeoutError:
            self._logger.error(
                f"IOP token 启动预热超时 | timeout={timeout:.1f}s | "
                f"不阻塞服务启动，首个请求将按需刷新")
        except Exception as e:
            self._logger.error(
                f"IOP token 启动预热异常 | error={type(e).__name__}: {e}")
        if self._tokens.token_valid():
            self._logger.info(
                f"IOP token 启动预热成功 | ttl={self._tokens.refresh_due_in():.0f}s")
        else:
            self._logger.error(
                "IOP token 启动预热未取得有效 token | 首个请求将按需刷新")

    def _start_background_refresh(self):
        cfg = self._config.background_refresh
        if not cfg.enabled:
            self._logger.info("IOP 后台 token 续期已关闭（按 .env 配置）")
            return
        if self._background_task is not None and not self._background_task.done():
            return
        self._background_task = asyncio.create_task(
            self._background_refresh_loop(), name="iop-token-refresh")
        self._logger.info(
            f"IOP 后台 token 续期已启动 | lead={cfg.lead:.0f}s | "
            f"min_interval={cfg.min_interval:.0f}s")

    async def _stop_background_refresh(self):
        task = self._background_task
        if task is None:
            return
        self._background_task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._logger.info("IOP 后台 token 续期已停止（lifespan shutdown）")

    async def _background_refresh_loop(self):
        """在 deadline 前 lead 秒续期；失败由 ensure_token 自己退避与冷却。"""
        cfg = self._config.background_refresh
        try:
            while True:
                due = self._tokens.refresh_due_in()
                wait = (max(cfg.min_interval, due - cfg.lead)
                        if due > 0 else cfg.min_interval)
                await asyncio.sleep(wait)
                due = self._tokens.refresh_due_in()
                if due <= 0 or due <= cfg.lead:
                    self._logger.info(
                        f"IOP 后台 token 续期触发 | due={due:.0f}s")
                    await self._tokens.ensure_token()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._logger.error(
                f"IOP 后台 token 续期异常退出 | error={type(e).__name__}: {e} | "
                f"请求路径仍会按需刷新")

    # ------------------------------------------------------------------
    # 标签查询
    # ------------------------------------------------------------------
    async def get_customer_labels(self, tel: str) -> dict:
        """查询客户标签画像；任何失败统一降级为 ``{}``（保持原契约）。

        需要知道「为什么是空」时用 ``get_customer_labels_with_status()``。
        """
        labels, _ = await self.get_customer_labels_with_status(tel)
        return labels

    async def get_customer_labels_with_status(self, tel: str) -> tuple[dict, str]:
        """查询标签并返回 ``(labels, status)``；``status`` 见 ``labels.py``。

        标签端点明确返回「token 失效」时，invalidate → 重刷一次 → 用新
        token 重试一次标签查询。
        """
        await self._tokens.ensure_token()

        if not self._tokens.token_valid():
            # 统一降级为空 dict：返回 None 不会触发上层的 except，而会在调用方
            # 按 dict 使用时抛 TypeError，反而让最该降级的场景失败。
            self._logger.error(
                "IOP token 不可用：刷新后仍无有效 access_token，本次标签查询降级为空标签")
            return {}, STATUS_TOKEN_UNAVAILABLE

        labels, status, auth_failed = await self._query_labels_once(tel)

        cfg = self._config.auth_failure
        if auth_failed and cfg.retry_enabled:
            self._logger.error(
                f"IOP 标签查询疑似 token 失效 | status={status} | "
                f"将 invalidate + 重刷一次 + 重试一次标签查询")
            self._tokens.invalidate("labels_auth_failure")
            # force=True：跳过失败冷却期短路，认证失效是新信息，值得立即再试。
            await self._tokens.ensure_token(force=True)
            if self._tokens.token_valid():
                self._logger.info("IOP token 失效自动重刷成功，重试一次标签查询")
                labels2, status2, _ = await self._query_labels_once(tel)
                if status2 in ("ok", "ok_empty") or labels2:
                    return labels2, status2
                self._logger.error(
                    f"IOP 重刷后标签查询仍失败 | status={status2} | 本次降级为空标签")
                return labels2, status2
            self._logger.error(
                "IOP token 失效自动重刷失败 | 本次标签查询降级为空标签")
            return {}, STATUS_TOKEN_UNAVAILABLE

        return labels, status

    async def _query_labels_once(self, tel: str) -> tuple[dict, str, bool]:
        """发一次标签请求，返回 ``(labels, status, auth_failed)``。"""
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
                f"IOP 标签查询网络失败 | error={type(e).__name__}: {e} | "
                f"cost={cost_ms:.0f}ms | reason={STATUS_NETWORK_ERROR}"
            )
            return {}, STATUS_NETWORK_ERROR, False

        status_code = resp.status
        try:
            # 上游返回的 Content-Type 不规范，不做类型校验
            res_data = await resp.json()
        except Exception as e:
            self._logger.error(
                f"IOP 标签查询响应解析失败 | status={status_code} | error={e} | "
                f"reason={STATUS_PARSE_ERROR}")
            return {}, STATUS_PARSE_ERROR, False

        cost_ms = (time.perf_counter() - start) * 1000
        labels, status, auth_failed = parse_custinfo_response_ex(
            res_data, status_code=status_code,
            logger=self._logger, cost_ms=cost_ms,
            auth_cfg=self._config.auth_failure)
        return labels, status, auth_failed
