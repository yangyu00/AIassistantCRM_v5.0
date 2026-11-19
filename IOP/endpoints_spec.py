# -*- coding: utf-8 -*-
"""把 ``IOPConfig`` 接到 ``endpoints.py`` 的薄适配层。

契约常量只在 ``endpoints.py`` 一处——本文件不复制任何路径、参数名或请求头。
``EndpointSpec`` 只持有配置对象并把两个请求方法委托过去；``IOPConfig`` 冻结，
换地址或凭据时重新构造一次即可，不需要在这里同步任何字段。

保留这层的原因：``TokenManager`` 只需要「给我 URL / body」，不该知道配置对象
长什么样，后续注入假的请求构造器也更方便。
"""
from __future__ import annotations

from . import endpoints
from .config import IOPConfig


class EndpointSpec:
    """由 ``IOPConfig`` 生成两端请求参数。"""

    def __init__(self, config: IOPConfig | None = None) -> None:
        self._config = config if config is not None else IOPConfig.from_env()

    @property
    def config(self) -> IOPConfig:
        return self._config

    def token_request(self):
        """返回 ``(url, body)``。"""
        return endpoints.token_request(self._config)

    def custinfo_request(self, tel: str, access_token: str):
        """返回 ``(url, headers, post_data)``。"""
        return endpoints.custinfo_request(self._config, tel, access_token)
