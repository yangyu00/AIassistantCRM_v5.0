# -*- coding: utf-8 -*-
"""IOP 两端点请求构造：把 ``IOPConfig`` 适配成 ``endpoints.py`` 期望的输入。

契约常量只在 ``endpoints.py`` 一处——本文件不复制任何路径、参数名或请求头，
而是复用它的公开 ``token_request()`` / ``custinfo_request()``。

``IOPConfig`` 是不可变 dataclass，而两个请求函数接收嵌套 dict。``EndpointSpec``
在内部构造这份 view，不改配置对象、不读外部文件；换地址或凭据时 view 自动
跟着变。
"""
from __future__ import annotations

from typing import Any

from . import endpoints
from .config import IOPConfig


class EndpointSpec:
    """由 ``IOPConfig`` 生成两端请求参数。

    ``token_request()`` / ``custinfo_request()`` 直接委托给 ``endpoints.py``，
    输出形状由那里唯一决定。
    """

    def __init__(self, config: IOPConfig | None = None) -> None:
        self._config = config if config is not None else IOPConfig.from_module_defaults()
        self._view = self._build_view(self._config)

    @staticmethod
    def _build_view(cfg: IOPConfig) -> dict[str, Any]:
        return {
            "IOP": {
                "TOKEN": {
                    "host": cfg.token.host,
                    "port": cfg.token.port,
                    "id": cfg.token.id,
                    "secret": cfg.token.secret,
                },
                "CustInfo": {
                    "host": cfg.cust_info.host,
                    "port": cfg.cust_info.port,
                    "version": cfg.cust_info.version,
                    "type": cfg.cust_info.type,
                },
            }
        }

    @property
    def config(self) -> IOPConfig:
        return self._config

    def token_request(self):
        """返回 ``(url, body)``。"""
        return endpoints.token_request(self._view)

    def custinfo_request(self, tel: str, access_token: str):
        """返回 ``(url, headers, post_data)``。"""
        return endpoints.custinfo_request(self._view, tel, access_token)
