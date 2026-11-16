# -*- coding: utf-8 -*-
"""IOP 两个端点共用的 aiohttp 会话及其生命周期。

应用级只创建一个 ``ClientSession``：``main.py`` 的 lifespan 在 startup 创建、
shutdown 关闭，二者都运行在 uvicorn 的**同一个 event loop** 内，避免每次请求
重新建连、TCP 握手与句柄开销。``atexit`` 拿不到运行中的 loop，不能用于收尾。

未经 lifespan 时（TestClient 不进上下文、独立脚本 import 后直接调用），
``session_context()`` 降级为「本次请求一个临时 session」，退出即关闭，
只告警一次；临时 session 同样在当前 event loop 内创建/关闭，不跨 loop。
"""
import asyncio
from contextlib import asynccontextmanager

import aiohttp
from config import ImmutableYamlConfig
from src.log_util import my_log

# 配置在 import 时读一次；get_token.py / get_custinfo.py 复用同一份只读配置，
# 比原先各读一次更省，请求路径仍然 0 次磁盘 I/O（E1）。
global_config = ImmutableYamlConfig()

_DEFAULT_TIMEOUT = {'total': 3.0, 'connect': 1.0, 'sock_read': 2.0}


def client_timeout() -> aiohttp.ClientTimeout:
    """IOP 两个端点共用的 HTTP 超时。

    从模块级 ``global_config`` 读取 ``IOP.timeout``，缺键用
    ``_DEFAULT_TIMEOUT`` 兜底，部署沿用旧 config.yaml 也不会启动失败。
    配置只读，本函数无磁盘 I/O。
    """
    cfg = global_config['IOP'].get('timeout', {}) or {}
    return aiohttp.ClientTimeout(
        total=float(cfg.get('total', _DEFAULT_TIMEOUT['total'])),
        connect=float(cfg.get('connect', _DEFAULT_TIMEOUT['connect'])),
        sock_read=float(cfg.get('sock_read', _DEFAULT_TIMEOUT['sock_read'])),
    )


# 共享 session 与其创建时的 event loop；只由 lifespan 在同一个 loop 内创建/关闭。
_SESSION = None
_LOOP = None
_WARNED = False


def _session_kwargs() -> dict:
    """共享 session 与降级临时 session 共用的构造参数。

    ``skip_auto_headers``：``requests`` 对字符串 ``data=`` 不加 Content-Type，
    而 aiohttp 默认补 ``text/plain``；跳过自动头后 token 请求与原来一致。
    它只跳过**自动生成**的头，调用点显式传入的 headers（标签查询的
    ``charset=gbk`` / ``Accept-Encoding``）不受影响，仍按原样发出（E3/E4）。
    两个端点共用一个 session 正是靠这一点，无需按端点拆两个 session。
    """
    return dict(timeout=client_timeout(),
                skip_auto_headers=("Content-Type",))


async def init_session():
    """lifespan startup 调用：在当前 event loop 创建共享 session。幂等。

    构造失败只记日志、不外抛：服务照常启动，后续请求由 ``session_context()``
    降级为临时 session。
    """
    global _SESSION, _LOOP
    if _SESSION is not None and not _SESSION.closed:
        return _SESSION
    try:
        _SESSION = aiohttp.ClientSession(**_session_kwargs())
        _LOOP = asyncio.get_running_loop()
    except Exception as e:
        _SESSION, _LOOP = None, None
        my_log.error(f"IOP 共享 ClientSession 创建失败，将按请求降级为临时 session | "
                     f"error={type(e).__name__}: {e}")
        return None
    my_log.info("IOP 共享 ClientSession 已创建（lifespan startup，同 event loop）")
    return _SESSION


async def close_session():
    """lifespan shutdown 调用：关闭共享 session。幂等，异常不外抛。"""
    global _SESSION, _LOOP
    if _SESSION is None:
        return
    loop = asyncio.get_running_loop()
    if _LOOP is not None and loop is not _LOOP:
        my_log.error("IOP 共享 ClientSession 关闭时的 event loop 与创建时不同，仍尝试关闭")
    session, _SESSION, _LOOP = _SESSION, None, None
    try:
        await session.close()
        my_log.info("IOP 共享 ClientSession 已关闭（lifespan shutdown）")
    except Exception as e:
        my_log.error(f"IOP 共享 ClientSession 关闭失败 | error={type(e).__name__}: {e}")


def get_session():
    """返回当前共享 session（未初始化时为 None），供自检与测试用。"""
    return _SESSION


@asynccontextmanager
async def session_context():
    """两个端点统一取会话的上下文。

    共享 session 已就绪时直接 yield，**绝不在此关闭**；否则降级为临时 session，
    由内层 ``async with`` 在当前 loop 内创建并关闭。降级只告警一次避免刷屏。
    """
    global _WARNED
    session = _SESSION
    if session is not None and not session.closed:
        yield session
        return
    if not _WARNED:
        _WARNED = True
        my_log.warning(
            "IOP 共享 ClientSession 未初始化（未经 lifespan？），本次请求降级为"
            "临时 session；请确认 main.py 的 lifespan 已执行"
        )
    async with aiohttp.ClientSession(**_session_kwargs()) as temp:
        yield temp
