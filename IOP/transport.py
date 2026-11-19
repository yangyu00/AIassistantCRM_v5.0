# -*- coding: utf-8 -*-
"""IOP 的 HTTP 传输层：把 aiohttp 会话与请求收口到一个可替换的对象里。

* ``Transport`` 是最小协议，只有一个 ``post()``。任何满足该协议的对象都能
  注入（例如内存里的假响应），不需要真实网络或全局状态。
* ``AiohttpTransport`` 是唯一依赖 aiohttp 的地方，持有共享 ``ClientSession``
  的生命周期、``skip_auto_headers`` 与临时 session 降级逻辑。
* ``HttpResponse`` 在退出 aiohttp 响应上下文之前把 JSON 读好，再以
  ``async json()`` 暴露；解析异常存下来由 ``json()`` 重抛，让上层按自己的
  分支处理。
* 日志由构造时注入；未注入 → ``NullLogger``，零输出。import 本文件不创建
  目录、不读外部配置。

请求形状（url / data / json / headers）由调用方按 ``endpoints.py`` 的契约
传入，本层不改写。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import aiohttp

from .config import IOPConfig, TimeoutCfg


# ---------------------------------------------------------------------------
# 日志：创建对象时注入，未注入则不记录日志
# ---------------------------------------------------------------------------
class NullLogger:
    """``info`` / ``error`` / ``warning`` 全为空操作，零副作用。

    IOP 只要求 logger 有这三个方法，``loguru`` 的 ``my_log``、
    ``logging.Logger``、自定义收集器都能直接传入，不需要包装类。
    """

    def info(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass


# ---------------------------------------------------------------------------
# 最小响应对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HttpResponse:
    """``post()`` 的结果：状态码 + 已读好的 JSON（或解析异常）。

    aiohttp 的响应对象在退出 ``async with`` 后就读不到 body 了，因此
    ``post()`` 必须在上下文内完成解析。解析异常存进 ``parse_error``，由
    ``json()`` 重抛：本层不吞异常，上层可以按自己的失败分支处理。

    ``FakeTransport`` 可直接构造：``HttpResponse(status=200, payload={...})``
    或 ``HttpResponse(status=502, parse_error=ValueError("..."))``。
    """

    status: int
    payload: Any = None
    parse_error: BaseException | None = None

    async def json(self):
        """返回已解析的 JSON；解析失败时重抛原异常对象。"""
        if self.parse_error is not None:
            raise self.parse_error
        return self.payload


# ---------------------------------------------------------------------------
# 传输协议
# ---------------------------------------------------------------------------
@runtime_checkable
class Transport(Protocol):
    """IOP 需要的唯一传输能力。

    参数与 ``aiohttp.ClientSession.post`` 的子集一致：``data`` 用于 token
    的表单串，``json`` / ``headers`` 用于标签查询。实现纯内存传输层也只需
    满足这一个方法。
    """

    async def post(
        self,
        url: str,
        *,
        data: Any = None,
        json: Any = None,
        headers: Any = None,
    ) -> HttpResponse:
        ...


# ---------------------------------------------------------------------------
# 默认实现：aiohttp 共享会话
# ---------------------------------------------------------------------------
class AiohttpTransport:
    """共享 aiohttp ``ClientSession`` 及其生命周期。

    - ``start()`` / ``aclose()`` 对应 lifespan 的 startup / shutdown，幂等、
      异常不外抛；共享 session 必须在**同一个 event loop** 内创建/关闭。
    - ``session_context()`` 共享 session 已就绪时直接 yield 且**绝不在此关闭**；
      否则降级为本次请求临时 session，只告警一次。
    - ``post()`` 收口一次请求，返回 ``HttpResponse``。网络异常原样向外抛，
      由上层就地捕获记录；本层不吞异常。
    """

    def __init__(self, config: IOPConfig | None = None, logger: Any = None) -> None:
        # config=None → 读 IOP/.env；logger=None → NullLogger（完全静默）
        self._config = config if config is not None else IOPConfig.load()
        self._logger = logger if logger is not None else NullLogger()

        # 共享会话状态，全部为实例字段
        self._session = None
        self._loop = None
        self._warned = False

    # -- 会话构造参数 --------------------------------------------------------
    def _session_kwargs(self) -> dict:
        """共享 session 与降级临时 session 共用的构造参数。

        ``skip_auto_headers``：跳过 aiohttp 自动补的 Content-Type。token 请求用
        字符串 ``data=``，按上游约定不带 Content-Type；标签查询则显式传入
        ``charset=gbk`` / ``Accept-Encoding``。该参数只跳过**自动生成**的头，
        显式传入的 headers 不受影响，仍按原样发出。两个端点共用一个 session
        正是靠这一点，无需按端点拆两个 session。
        """
        t: TimeoutCfg = self._config.timeout
        return dict(
            timeout=aiohttp.ClientTimeout(
                total=t.total, connect=t.connect, sock_read=t.sock_read),
            skip_auto_headers=("Content-Type",),
        )

    def client_timeout(self) -> aiohttp.ClientTimeout:
        """返回本实例使用的超时对象（与共享 session 用同一份构造参数）。"""
        return self._session_kwargs()["timeout"]

    # -- 生命周期 ------------------------------------------------------------
    async def start(self):
        """在当前 event loop 创建共享 session，幂等。

        构造失败只记日志、不外抛：服务照常启动，后续请求由
        ``session_context()`` 降级为临时 session。
        """
        if self._session is not None and not self._session.closed:
            return self._session
        try:
            self._session = aiohttp.ClientSession(**self._session_kwargs())
            self._loop = asyncio.get_running_loop()
        except Exception as e:
            self._session, self._loop = None, None
            self._logger.error(
                f"IOP 共享 ClientSession 创建失败，将按请求降级为临时 session | "
                f"error={type(e).__name__}: {e}")
            return None
        self._logger.info(
            "IOP 共享 ClientSession 已创建（lifespan startup，同 event loop）")
        return self._session

    async def aclose(self):
        """关闭共享 session，幂等，异常不外抛。"""
        if self._session is None:
            return
        loop = asyncio.get_running_loop()
        if self._loop is not None and loop is not self._loop:
            self._logger.error(
                "IOP 共享 ClientSession 关闭时的 event loop 与创建时不同，仍尝试关闭")
        session, self._session, self._loop = self._session, None, None
        try:
            await session.close()
            self._logger.info(
                "IOP 共享 ClientSession 已关闭（lifespan shutdown）")
        except Exception as e:
            self._logger.error(
                f"IOP 共享 ClientSession 关闭失败 | error={type(e).__name__}: {e}")

    def get_session(self):
        """返回当前共享 session（未初始化时为 None），供自检与测试用。"""
        return self._session

    @asynccontextmanager
    async def session_context(self):
        """两个端点统一取会话的上下文。"""
        session = self._session
        if session is not None and not session.closed:
            yield session
            return
        if not self._warned:
            self._warned = True
            self._logger.warning(
                "IOP 共享 ClientSession 未初始化（未经 lifespan？），本次请求降级为"
                "临时 session；请确认 main.py 的 lifespan 已执行")
        async with aiohttp.ClientSession(**self._session_kwargs()) as temp:
            yield temp

    # -- 请求 ----------------------------------------------------------------
    async def post(
        self,
        url: str,
        *,
        data: Any = None,
        json: Any = None,
        headers: Any = None,
    ) -> HttpResponse:
        """发一次请求并收口响应，返回 ``HttpResponse``。

        * 网络异常（``aiohttp.ClientError`` / ``asyncio.TimeoutError``）原样向
          外抛，由上层就地捕获；本层不吞异常。
        * JSON 解析异常存进 ``HttpResponse.parse_error``，交给上层处理。
        * 只把非 ``None`` 的参数传给 aiohttp：token 请求只传 ``data``，
          标签请求只传 ``json`` + ``headers``。
        """
        kwargs = {}
        if data is not None:
            kwargs["data"] = data
        if json is not None:
            kwargs["json"] = json
        if headers is not None:
            kwargs["headers"] = headers

        async with self.session_context() as client:
            async with client.post(url, **kwargs) as response:
                status = response.status
                try:
                    # 上游返回的 Content-Type 不规范，不做类型校验
                    payload = await response.json(content_type=None)
                except Exception as e:
                    return HttpResponse(status=status, payload=None, parse_error=e)
                return HttpResponse(status=status, payload=payload, parse_error=None)
