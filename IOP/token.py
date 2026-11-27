"""token 的获取、有效期、并发保护与失效重刷。"""
from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any

import aiohttp

from . import endpoints
from .config import IOPConfig
from .transport import AiohttpTransport, HttpResponse, NullLogger, Transport

# 无有效 token 时 deadline 统一置为这个过去时间点，保证 state.deadline
# 恒为 datetime，`now <= deadline` 永远可比较。
_INVALID_DEADLINE = datetime.datetime.min


class TokenState:
    """token 值与有效期的实例状态。"""

    def __init__(self) -> None:
        self.access_token = None
        self.deadline = _INVALID_DEADLINE


class TokenManager:
    """token 值、有效期、单飞刷新、重试退避冷却、响应校验。"""

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

        self._state = TokenState()

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

    @property
    def state(self) -> TokenState:
        """唯一的对外状态视图。"""
        return self._state

    def effective_lifetime(self, raw):
        """把上游 ``expires_in`` 换算成写入 ``state.deadline`` 的实际秒数。

        注意顺序：先把 ``v`` 夹到 ``max_expires_in``，再减 ``refresh_buffer``。
        所以「600 − 360 = 240s」只是上游缺字段 / 非法值回落到 default=600 时
        的结果，不是固定有效期；上游给 600000 秒时实际 ttl 接近 7 天。
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

    def _get_refresh_lock(self) -> asyncio.Lock:
        """返回绑定当前 event loop 的单飞锁。"""
        loop = asyncio.get_running_loop()
        if self._refresh_lock is None or self._refresh_lock_loop is not loop:
            self._refresh_lock = asyncio.Lock()
            self._refresh_lock_loop = loop
        return self._refresh_lock

    def token_valid(self) -> bool:
        return (self._state.access_token is not None
                and datetime.datetime.now() <= self._state.deadline)

    def refresh_due_in(self) -> float:
        """距离 deadline 还剩多少秒；无有效 token 返回 0。后台续期用。"""
        if not self.token_valid():
            return 0.0
        return max(0.0, (self._state.deadline
                         - datetime.datetime.now()).total_seconds())

    def invalidate(self, reason: str = "manual") -> None:
        """清空 token 状态，强制下次 ``ensure_token()`` 重新刷新。

        标签端点明确返回「token 失效」时调用。
        """
        if self._state.access_token is None:
            return
        self._state.access_token = None
        self._state.deadline = _INVALID_DEADLINE
        self._logger.info(f"IOP token 已失效待重刷 | reason={reason}")

    async def ensure_token(self, *, force: bool = False):
        """确保 state 里有有效 access_token，业务侧统一调用本方法。

        ``force=True`` 用于「标签端点已明确告知 token 失效」的场景：跳过失败
        冷却期的短路，立即尝试重刷一次，但仍走单飞锁与重试退避保护。
        """
        if self.token_valid():
            return

        now = time.monotonic()
        if not force and now < self._cooldown_until:
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
                    or (not force and time.monotonic() < self._cooldown_until)):
                return

            n = self._config.retry.max_attempts
            start = time.perf_counter()
            self._logger.info(
                f"IOP token 刷新开始 | max_attempts={n} | force={force}")
            try:
                for attempt in range(1, n + 1):
                    # refresh_once() 返回「本轮是否写入了新 token」。续期失败但
                    # 保留了旧 token 时 token_valid() 仍为 True，不能据此判定刷新
                    # 成功，否则会把「保留旧 token」误记成「刷新成功」。
                    refreshed = await self.refresh_once()
                    if refreshed and self.token_valid():
                        self._logger.info(
                            f"IOP token 刷新成功 | attempt={attempt}/{n} | "
                            f"cost={(time.perf_counter() - start) * 1000:.0f}ms"
                        )
                        self._cooldown_until = 0.0
                        return
                    if attempt < n:
                        delay = self._config.retry.backoff_seconds(attempt)
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
                    f"cost={(time.perf_counter() - start) * 1000:.0f}ms | "
                    f"old_token_kept={self.token_valid()}"
                )
            finally:
                self._refresh_gen += 1

    def _fail_token(self, status_code, reason, *, keep_valid: bool = False):
        """所有失败分支统一收口。

        ``keep_valid=True`` 用于「续期刷新」：若旧 token 仍在有效期内，刷新
        失败只记 ERROR 并保留旧 token，不把它清空——否则一次上游抖动就会让
        整个进程立刻无 token 可用（后台刷新尤其容易踩到）。
        首次获取时 state 本就没有 token，保留逻辑自然不生效。
        """
        if keep_valid and self.token_valid():
            self._logger.error(
                f"IOP token 刷新失败但保留仍有效的旧 token | status={status_code} | "
                f"remain={self.refresh_due_in():.0f}s | {reason}")
            return
        self._state.access_token = None
        self._state.deadline = _INVALID_DEADLINE
        self._logger.error(f"IOP token 请求失败 | status={status_code} | {reason}")

    def _warn_if_suspicious(self, raw):
        """疑似毫秒 / 绝对时间戳时显式告警，避免按「秒」误判出超长有效期。"""
        try:
            if isinstance(raw, bool):
                return
            v = float(raw)
        except (TypeError, ValueError):
            return
        if v > 1e9:
            self._logger.error(
                f"IOP expires_in 疑似绝对时间戳或毫秒而非秒数 | raw={raw!r} | "
                f"请抓一次 token 端点原始响应确认单位（见 tests/check_iop_token_raw.py）")

    async def refresh_once(self) -> bool:
        """单次刷新 token 并写入实例 state；返回是否写入了**新** token。"""
        url, body = endpoints.token_request(self._config)

        # 上游仅提供 http 明文 token 端点；client_id / client_secret 以表单串提交。
        # 下面只发请求；会话参数由 transport 持有。
        status_code = None
        try:
            resp: HttpResponse = await self._transport.post(url, data=body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # 超时/网络异常就地收口，不让异常穿透到上层（否则 IOP 侧没有日志）
            self._fail_token(status_code, f"error={type(e).__name__}: {e}",
                             keep_valid=True)
            return False

        status_code = resp.status
        # 先查状态码。502 / 网关错误页即使 body 是合法 JSON，
        # 也不该进入「按字段判断」的路径；不读 body 直接失败。
        if status_code != 200:
            self._fail_token(status_code, "HTTP 状态码非 200", keep_valid=True)
            return False
        try:
            # 上游返回的 Content-Type 不规范，不做类型校验
            res_data = await resp.json()
        except (aiohttp.ClientError, ValueError) as e:
            self._fail_token(status_code,
                             f"响应解析失败 | error={type(e).__name__}: {e}",
                             keep_valid=True)
            return False

        # res_data 可能是 list / 字符串 / None（HTML 错误页被强行解析时也可能如此）
        if not isinstance(res_data, dict):
            self._fail_token(status_code,
                             f"响应结构异常 | type={type(res_data).__name__}",
                             keep_valid=True)
            return False

        if 'access_token' in res_data:
            access_token = res_data['access_token']
            if not access_token:
                self._fail_token(status_code, "access_token 为空", keep_valid=True)
                return False

            p = self._config.lifetime
            # 有效期字段支持别名：上游若不用标准 expires_in（如 expiresIn），
            fields = self._config.expires_in_fields or ("expires_in",)
            field_used, raw = None, None
            for name in fields:
                if name in res_data:
                    field_used, raw = name, res_data[name]
                    break

            if field_used is None:
                lifetime, _ = self.effective_lifetime(p.default_expires_in)
                self._logger.error(
                    f"IOP token 响应缺少有效期字段 | tried={','.join(fields)} | "
                    f"keys={','.join(sorted(res_data))} | raw=缺失 | "
                    f"按 default={p.default_expires_in:.0f}s 计算 ttl={lifetime:.0f}s | "
                    f"若因此每 4 分钟刷新一次，请核对上游字段名与单位")
                raw = p.default_expires_in
            else:
                lifetime, note = self.effective_lifetime(raw)
                if field_used != "expires_in":
                    self._logger.info(
                        f"IOP token 有效期字段使用别名 | field={field_used} | raw={raw!r}")
                if note:
                    # 用 ERROR 而非 INFO：这是上游契约异常，必须落盘可见。
                    # configure_logging() 只挂 INFO / ERROR 文件 sink。
                    self._logger.error(
                        f"IOP token expires_in 异常 | {note} | raw={raw!r} | "
                        f"按 {lifetime:.0f}s 计算有效期")
                self._warn_if_suspicious(raw)

            # 顺序不变：先写 deadline 再写 access_token
            self._state.deadline = (datetime.datetime.now()
                                    + datetime.timedelta(seconds=lifetime))
            self._state.access_token = access_token
            # 记录上游原始值与本地实际 ttl：排查「有效期怎么这么短 / 这么长」
            # 时不必再猜单位。
            self._logger.info(
                f"IOP token 已写入 | field={field_used} | raw_expires_in={raw!r} | "
                f"ttl={lifetime:.0f}s ({lifetime / 3600:.2f}h) | "
                f"max_expires_in={p.max_expires_in:.0f}s")
            return True

        if 'error' in res_data:
            self._fail_token(
                status_code,
                f"上游返回错误 | error={res_data.get('error')} | "
                f"error_description={res_data.get('error_description', '')}",
                keep_valid=True)
            return False

        # 既无 access_token 也无 error（如网关返回的业务错误 JSON）。
        # 续期场景下若旧 token 仍在有效期内则保留（keep_valid=True），只有
        # 标签端点明确告知失效时才由 invalidate() 清空。
        self._fail_token(status_code, "响应缺少 access_token 且无 error 字段",
                         keep_valid=True)
        return False
