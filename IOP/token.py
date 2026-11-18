# -*- coding: utf-8 -*-
"""token 的获取、有效期与并发保护。

``TokenState`` 持有 token 值与有效期，``TokenManager`` 负责单飞刷新、重试
退避、失败冷却与响应校验。全部状态都在实例内，不同实例互不干扰。

并发规则：
* ``asyncio.Lock`` 按当前 event loop 懒创建/重建，跨 ``asyncio.run()`` 安全；
* 拿锁前后各检查一次 token 有效性，并用刷新代次让同批并发共享一轮结果；
* 失败冷却在拿锁之前短路，一个冷却周期只打一条 INFO 提示。

参数取自 ``IOPConfig``；HTTP 通过注入的 ``Transport`` 发出；日志由构造时
注入，未注入则完全静默。
"""
from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any

import aiohttp

from .config import IOPConfig
from .endpoints_spec import EndpointSpec
from .transport import AiohttpTransport, HttpResponse, NullLogger, Transport

# 无有效 token 时 deadline 统一置为这个过去时间点，保证 state.deadline
# 恒为 datetime，`now <= deadline` 永远可比较。
_INVALID_DEADLINE = datetime.datetime.min


class TokenState:
    """token 值与有效期的实例状态。

    不变量：``deadline`` 恒为 ``datetime``（初始/失败均为
    ``_INVALID_DEADLINE``）；``access_token is not None`` 是「拿没拿到」的
    业务条件。
    """

    def __init__(self) -> None:
        self.access_token = None
        self.deadline = _INVALID_DEADLINE


class TokenManager:
    """token 值、有效期、单飞刷新、重试退避冷却、响应校验。

    构造注入点：``config`` / ``transport`` / ``spec`` / ``logger``，全部可选。
    ``config=None`` → 模块内默认常量；``logger=None`` → 完全静默；
    ``transport=None`` → 自建 ``AiohttpTransport``；``spec=None`` → 自建
    ``EndpointSpec``。每个实例状态独立，天然隔离，无需任何全局复位。
    """

    def __init__(
        self,
        config: IOPConfig | None = None,
        transport: Transport | None = None,
        spec: EndpointSpec | None = None,
        logger: Any = None,
    ) -> None:
        self._config = config if config is not None else IOPConfig.from_module_defaults()
        self._logger = logger if logger is not None else NullLogger()
        self._transport = (transport if transport is not None
                           else AiohttpTransport(self._config, self._logger))
        self._spec = spec if spec is not None else EndpointSpec(self._config)

        self._state = TokenState()

        # 原模块级全局 → 实例字段
        # 冷却截止点用 time.monotonic()，不受系统时间回拨影响；0 表示不在冷却期。
        self._cooldown_until = 0.0
        # 同一个冷却周期只打一条 INFO 提示，避免每个失败请求都刷屏。
        self._cooldown_warned_until = 0.0
        # asyncio.Lock 一旦争用就绑定创建它的 event loop，跨 loop 会抛
        # RuntimeError。按当前 loop 懒创建/重建，不放在模块级裸锁。
        self._refresh_lock = None
        self._refresh_lock_loop = None
        # 刷新「代次」：每完成一轮刷新（成功或失败）+1。并发请求在等锁前记下
        # 代次，拿锁后若代次已变，说明已有请求替大家刷过一轮，直接复用结果。
        self._refresh_gen = 0

    # -- 对外只读视图 -------------------------------------------------------
    @property
    def state(self) -> TokenState:
        return self._state

    @property
    def tokens(self) -> TokenState:
        """``state`` 的别名。"""
        return self._state

    @property
    def config(self) -> IOPConfig:
        return self._config

    @property
    def retry(self):
        """重试与退避策略。"""
        return self._config.retry

    @property
    def lifetime(self):
        """token 有效期换算参数。"""
        return self._config.lifetime

    @property
    def transport(self):
        return self._transport

    def backoff_seconds(self, attempt: int) -> float:
        """第 attempt 次失败后的退避秒数（从 1 起），指数增长并封顶。"""
        return self._config.retry.backoff_seconds(attempt)

    # -- 有效期换算 ----------------------------------------------------------
    def effective_lifetime(self, raw):
        """把上游 ``expires_in`` 换算成写入 ``state.deadline`` 的实际秒数。

        返回 ``(seconds, note)``；note 为 None 表示值正常。
        - 非数字 / bool / NaN / inf / <= 0 → 回落 ``default_expires_in``
        - 超过 ``max_expires_in`` → 截断到上限
        - 有效期 = ``min(max(上游值 - refresh_buffer, min_lifetime), 上游值)``
        """
        p = self._config.lifetime
        note = None
        try:
            if isinstance(raw, bool):
                raise ValueError('bool is not a lifetime')
            v = float(raw)
        except (TypeError, ValueError):
            v = p.default_expires_in
            note = f"非数字({raw!r})"
        if v != v or v in (float('inf'), float('-inf')) or v <= 0:
            v = p.default_expires_in
            note = note or f"非有限或非正({raw!r})"
        if v > p.max_expires_in:
            v = p.max_expires_in
            note = note or f"超过上限({raw!r} > {p.max_expires_in:.0f}s)"
        ttl = min(max(v - p.refresh_buffer, p.min_lifetime), v)
        return ttl, note

    # -- 单飞锁 ---------------------------------------------------------------
    def _get_refresh_lock(self) -> asyncio.Lock:
        """返回绑定当前 event loop 的单飞锁；跨 loop 自动重建。"""
        loop = asyncio.get_running_loop()
        if self._refresh_lock is None or self._refresh_lock_loop is not loop:
            self._refresh_lock = asyncio.Lock()
            self._refresh_lock_loop = loop
        return self._refresh_lock

    def token_valid(self) -> bool:
        """token 是否有效。

        ``deadline`` 恒为 datetime（无有效 token 时是 ``_INVALID_DEADLINE``），
        ``now <= deadline`` 可直接比较。``access_token is not None`` 是
        「拿没拿到 token」的业务条件。
        """
        return (self._state.access_token is not None
                and datetime.datetime.now() <= self._state.deadline)

    # -- 编排入口 -----------------------------------------------------------
    async def ensure_token(self):
        """确保 state 里有有效 access_token，业务侧统一调用本方法。

        不要直接调用 ``token2global()``，否则绕开并发保护。
        - token 已有效：快速路径直接返回，连锁都不碰
        - 冷却期内：直接返回（不刷新），调用方按「无标签」降级；
          一个冷却周期只打一条 INFO 提示（本项目 WARNING 不落盘）
        - 并发同时判定过期：只有第一个请求真正刷新，其余等锁后二次检查复用结果
        - 单轮刷新：max_attempts 次请求，成功即返回；失败之间指数退避并封顶
        - 单轮全失败：进入 cooldown 冷却，刷新失败不抛异常
        """
        if self.token_valid():
            return

        now = time.monotonic()
        if now < self._cooldown_until:
            if self._cooldown_warned_until != self._cooldown_until:
                self._cooldown_warned_until = self._cooldown_until
                # 冷却是预期内的快速降级，用 INFO 提示，避免每个请求刷屏。
                self._logger.info(
                    f"IOP token 处于失败冷却期 | remain={self._cooldown_until - now:.1f}s | "
                    f"本轮请求直接降级为空标签"
                )
            return

        gen = self._refresh_gen
        async with self._get_refresh_lock():
            # 二次检查：别人已刷成功、已有人替本批并发刷过一轮（含失败），
            # 或持锁等待期间刚进入冷却期
            if (self.token_valid() or self._refresh_gen != gen
                    or time.monotonic() < self._cooldown_until):
                return

            n = self._config.retry.max_attempts
            start = time.perf_counter()
            self._logger.info(f"IOP token 刷新开始 | max_attempts={n}")
            try:
                for attempt in range(1, n + 1):
                    await self.token2global()
                    if self.token_valid():
                        self._logger.info(
                            f"IOP token 刷新成功 | attempt={attempt}/{n} | "
                            f"cost={(time.perf_counter() - start) * 1000:.0f}ms"
                        )
                        self._cooldown_until = 0.0
                        return
                    if attempt < n:
                        delay = self.backoff_seconds(attempt)
                        # 记 ERROR：这是一次失败尝试，便于与「IOP token 请求失败」
                        # 一起检索。
                        self._logger.error(
                            f"IOP token 刷新重试 | attempt={attempt}/{n} | "
                            f"backoff={delay:.2f}s"
                        )
                        await asyncio.sleep(delay)
                self._cooldown_until = time.monotonic() + self._config.retry.cooldown
                self._logger.error(
                    f"IOP token 刷新失败 | attempts={n} | "
                    f"cooldown={self._config.retry.cooldown:.1f}s | "
                    f"cost={(time.perf_counter() - start) * 1000:.0f}ms"
                )
            finally:
                self._refresh_gen += 1

    # -- 失败统一收口 -------------------------------------------------------
    def _fail_token(self, status_code, reason):
        """所有失败分支统一清空 state 并记 ERROR，绝不沿用可能已过期的 token 值。

        ``deadline`` 置回 ``_INVALID_DEADLINE``（过去时间），保持「恒为
        datetime」的不变量。日志前缀为 ``IOP token 请求失败``，附带 status
        与具体原因，级别用 ERROR。
        """
        self._state.access_token = None
        self._state.deadline = _INVALID_DEADLINE
        self._logger.error(f"IOP token 请求失败 | status={status_code} | {reason}")

    # -- 单次刷新原语 -------------------------------------------------------
    async def token2global(self):
        """单次刷新 token 并写入 state。

        只由 ``ensure_token()`` 在单飞锁内调用；业务侧请勿直接调用，否则绕开
        并发保护。先查状态码，再校验响应结构；所有失败分支统一走
        ``_fail_token()``，不让「既无 access_token 也无 error」的响应沿用
        已有的 token 值。
        """
        url, body = self._spec.token_request()

        # 上游仅提供 http 明文 token 端点；client_id / client_secret 以表单串提交。
        # 下面只发请求、不再构造会话参数（会话由 transport 持有）。
        status_code = None
        try:
            resp: HttpResponse = await self._transport.post(url, data=body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # 超时/网络异常就地收口，不让异常穿透到上层（否则 IOP 侧没有日志）
            self._fail_token(status_code, f"error={type(e).__name__}: {e}")
            return

        status_code = resp.status
        # 先查状态码。502 / 网关错误页即使 body 是合法 JSON，
        # 也不该进入「按字段判断」的路径；不读 body 直接失败。
        if status_code != 200:
            self._fail_token(status_code, "HTTP 状态码非 200")
            return
        try:
            # 上游返回的 Content-Type 不规范，不做类型校验
            res_data = await resp.json()
        except (aiohttp.ClientError, ValueError) as e:
            self._fail_token(status_code, f"响应解析失败 | error={type(e).__name__}: {e}")
            return

        # res_data 可能是 list / 字符串 / None（HTML 错误页被强行解析时也可能如此）
        if not isinstance(res_data, dict):
            self._fail_token(status_code, f"响应结构异常 | type={type(res_data).__name__}")
            return

        if 'access_token' in res_data:
            access_token = res_data['access_token']
            if not access_token:
                self._fail_token(status_code, "access_token 为空")
                return
            # expires_in 缺失时静默用默认值；字段存在但非法/过短/过大时
            # 由 effective_lifetime() 兜底或夹取，并记一条 INFO（token 本身有效，
            # 不算失败，故不用 ERROR）。
            if 'expires_in' in res_data:
                lifetime, note = self.effective_lifetime(res_data['expires_in'])
            else:
                lifetime, note = self.effective_lifetime(
                    self._config.lifetime.default_expires_in)
            if note:
                self._logger.info(
                    f"IOP token expires_in 异常 | {note} | "
                    f"按 {lifetime:.0f}s 计算有效期"
                )
            # 顺序不变：先写 deadline 再写 access_token
            self._state.deadline = (datetime.datetime.now()
                                    + datetime.timedelta(seconds=lifetime))
            self._state.access_token = access_token
            return

        if 'error' in res_data:
            self._fail_token(
                status_code,
                f"上游返回错误 | error={res_data.get('error')} | "
                f"error_description={res_data.get('error_description', '')}")
            return

        # 既无 access_token 也无 error（如网关返回的业务错误 JSON），
        # 不能沿用已有的、可能已过期的 access_token。
        self._fail_token(status_code, "响应缺少 access_token 且无 error 字段")
