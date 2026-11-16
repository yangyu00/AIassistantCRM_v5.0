# -*- coding: utf-8 -*-

import asyncio
import aiohttp
from config import ImmutableYamlConfig
from src.log_util import my_log
import datetime


class Tokens:
    def __init__(self) -> None:
        self.access_token = None
        self.deadline = None


TOKENS = Tokens()

# 配置在 import 时读一次，避免每次 token 刷新都重新打开并解析 config.yaml。
global_config = ImmutableYamlConfig()

# C1：config.yaml 缺少 IOP.timeout 段时的兜底值（秒）。
_DEFAULT_TIMEOUT = {'total': 3.0, 'connect': 1.0, 'sock_read': 2.0}


def client_timeout() -> aiohttp.ClientTimeout:
    """IOP 两个端点共用的 HTTP 超时。

    从模块级 global_config 读取 IOP.timeout，缺键用 _DEFAULT_TIMEOUT 兜底，
    部署沿用旧 config.yaml 也不会启动失败。配置只读，本函数无磁盘 I/O。
    """
    cfg = global_config['IOP'].get('timeout', {}) or {}
    return aiohttp.ClientTimeout(
        total=float(cfg.get('total', _DEFAULT_TIMEOUT['total'])),
        connect=float(cfg.get('connect', _DEFAULT_TIMEOUT['connect'])),
        sock_read=float(cfg.get('sock_read', _DEFAULT_TIMEOUT['sock_read'])),
    )


async def token2global():
    # 上游仅提供 http 明文 token 端点；client_id / client_secret 以表单串提交，
    # 凭据存放于 config.yaml。均为上游与部署环境的既定要求，不可改动。
    token_url = 'http://{host}:{port}/openapi/restOauth2Server/access_token'.format(
        host=global_config['IOP']['TOKEN']['host'], port=global_config['IOP']['TOKEN']['port'])

    body = (f"grant_type=client_credentials"
            f"&client_id={global_config['IOP']['TOKEN']['id']}"
            f"&client_secret={global_config['IOP']['TOKEN']['secret']}")

    try:
        async with aiohttp.ClientSession(
                timeout=client_timeout(),
                skip_auto_headers=("Content-Type",)) as client:
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
