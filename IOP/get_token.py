# -*- coding: utf-8 -*-

import asyncio
import aiohttp
from src.log_util import my_log
import datetime

# client_timeout / global_config / session_context 统一由 http_session 提供；
# 此处转出 client_timeout 以保持既有 `from IOP.get_token import client_timeout` 可用。
from IOP.http_session import global_config, client_timeout, session_context


class Tokens:
    def __init__(self) -> None:
        self.access_token = None
        self.deadline = None


TOKENS = Tokens()

# --- A2：token 单飞 -----------------------------------------------------------
# 与 C2 共享 ClientSession 同样的约束：asyncio.Lock 一旦发生过争用就绑定创建它的
# event loop，跨 loop 使用会抛 RuntimeError。生产是单进程单 loop，但 TestClient、
# 独立脚本可能多次 asyncio.run()，故按当前 loop 懒创建/重建，不放在模块级裸锁。
_REFRESH_LOCK = None
_REFRESH_LOCK_LOOP = None

# 刷新「代次」：每完成一轮刷新（成功或失败）+1。并发请求在等锁前记下代次，
# 拿锁后若代次已变，说明已有请求替大家刷过一轮，直接复用其结果，不再重复刷新。
# 这样刷新失败时也不会让 N 个等待者各刷一轮（那是把「并发打爆上游」变成
# 「串行 4N 次」，延迟反而更糟）。失败冷却/节流属于 D1，本轮不做。
_REFRESH_GEN = 0


def _get_refresh_lock() -> asyncio.Lock:
    """返回绑定当前 event loop 的单飞锁；跨 loop 自动重建。"""
    global _REFRESH_LOCK, _REFRESH_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _REFRESH_LOCK is None or _REFRESH_LOCK_LOOP is not loop:
        _REFRESH_LOCK = asyncio.Lock()
        _REFRESH_LOCK_LOOP = loop
    return _REFRESH_LOCK


def token_valid() -> bool:
    """token 是否有效。短路顺序保护 deadline 初始为 None（A3 未改，不改初始化）。"""
    return (TOKENS.access_token is not None
            and datetime.datetime.now() <= TOKENS.deadline)


async def ensure_token():
    """A2 单飞入口：确保 TOKENS 里有有效 access_token。

    业务侧统一调用本函数，不要直接调用 token2global()。
    - token 已有效：快速路径直接返回，连锁都不碰
    - 并发同时判定过期：只有第一个请求真正刷新，其余等锁后二次检查复用结果
    - 重试语义与改动前一致（D1 未改）：循环外 1 次 + 循环内最多 3 次、
      成功即 break、无退避；只是整体搬进锁内，由单飞保护
    - 刷新失败不抛异常，由调用方按 B1 统一降级为空 dict
    """
    global _REFRESH_GEN
    if token_valid():
        return
    gen = _REFRESH_GEN
    async with _get_refresh_lock():
        # 二次检查：别人已刷成功，或已有人替本批并发刷过一轮（含失败）
        if token_valid() or _REFRESH_GEN != gen:
            return
        try:
            await token2global()
            for _ in range(3):
                if token_valid():
                    break
                await token2global()
        finally:
            _REFRESH_GEN += 1


# ---------------------------------------------------------------------------


async def token2global():
    """单次刷新 token 并写入 TOKENS。

    只由 ensure_token() 在单飞锁内调用；业务侧请勿直接调用，否则绕开 A2 保护。
    """
    # 上游仅提供 http 明文 token 端点；client_id / client_secret 以表单串提交，
    # 凭据存放于 config.yaml。均为上游与部署环境的既定要求，不可改动。
    token_url = 'http://{host}:{port}/openapi/restOauth2Server/access_token'.format(
        host=global_config['IOP']['TOKEN']['host'], port=global_config['IOP']['TOKEN']['port'])

    body = (f"grant_type=client_credentials"
            f"&client_id={global_config['IOP']['TOKEN']['id']}"
            f"&client_secret={global_config['IOP']['TOKEN']['secret']}")

    # C2：复用应用级共享 session（lifespan 创建/关闭），不再每次新建。
    # session 已带 timeout 与 skip_auto_headers，故这里只发请求、不再构造参数；
    # 超时/网络异常就地收口，不让异常穿透到 main.py（否则 IOP 侧会没有日志）。
    try:
        async with session_context() as client:
            async with client.post(token_url, data=body) as response:
                res_data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        TOKENS.access_token = None
        my_log.error(f"IOP token 请求失败 | error={type(e).__name__}: {e}")
        return

    if 'access_token' in res_data:
        expires_in = res_data.get('expires_in', 600)
        TOKENS.deadline = datetime.datetime.now() + datetime.timedelta(seconds=(int(expires_in) - 360))
        TOKENS.access_token = res_data['access_token']
    elif 'error' in res_data:
        TOKENS.access_token = None
        my_log.error(str(res_data))
