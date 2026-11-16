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


async def token2global():
    # 上游仅提供 http 明文 token 端点；client_id / client_secret 以表单串提交，
    # 凭据存放于 config.yaml。均为上游与部署环境的既定要求，不可改动。
    token_url = 'http://{host}:{port}/openapi/restOauth2Server/access_token'.format(
        host=global_config['IOP']['TOKEN']['host'], port=global_config['IOP']['TOKEN']['port'])

    body = (f"grant_type=client_credentials"
            f"&client_id={global_config['IOP']['TOKEN']['id']}"
            f"&client_secret={global_config['IOP']['TOKEN']['secret']}")

    # C2：复用应用级共享 session（lifespan 创建/关闭），不再每次新建。
    # session 已带 timeout 与 skip_auto_headers，故这里只发请求、不再构造参数；
    # 超时/网络异常就地收口，不让异常穿透到 main.py（否则 IOP 侧没有日志）。
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
