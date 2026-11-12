# -*- coding: utf-8 -*-

import requests
from config import ImmutableYamlConfig
from src.log_util import my_log
import datetime


class Tokens:
    def __init__(self) -> None:
        self.access_token = None
        self.deadline = None


TOKENS = Tokens()


async def token2global():
    global_config = ImmutableYamlConfig()

    token_url = 'http://{host}:{port}/openapi/restOauth2Server/access_token'.format(
        host=global_config['IOP']['TOKEN']['host'], port=global_config['IOP']['TOKEN']['port'])

    response = requests.post(token_url, data=f"grant_type=client_credentials"
                                             f"&client_id={global_config['IOP']['TOKEN']['id']}"
                                             f"&client_secret={global_config['IOP']['TOKEN']['secret']}")
    res_data = response.json()

    if 'access_token' in res_data:
        expires_in = res_data.get('expires_in', 600)
        TOKENS.deadline = datetime.datetime.now() + datetime.timedelta(seconds=(int(expires_in) - 360))
        TOKENS.access_token = res_data['access_token']
    elif 'error' in res_data:
        TOKENS.access_token = None
        my_log.error(str(res_data))
