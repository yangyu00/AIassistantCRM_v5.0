# -*- coding: utf-8 -*-
"""IOP 两端点请求构造：把模块内 ``IOPConfig`` 适配成 ``endpoints.py`` 期望的形状。

第 2 步新增。方案 §4.1 原把 ``EndpointSpec`` 放进 ``endpoints.py``，但为遵守
「本步仅新增文件、存量 0 改动」，这里独立成文件；**契约常量仍只在
``endpoints.py`` 一处**——本文件不复制任何路径、参数名或请求头，而是复用
它的公开 ``token_request()`` / ``custinfo_request()``。第 4 步旧文件退化成
薄壳时若有需要可再归位。

``IOPConfig`` 是扁平 dataclass，而两个公开函数接收的是与 ``config.yaml``
的 ``IOP`` 段同形状的 dict。``EndpointSpec`` 只在自己内部构造这份 view，
不改配置对象、不读外部文件；换地址/凭据时 view 自动跟着变。
"""
from __future__ import annotations

from typing import Any

from . import endpoints
from .config import IOPConfig


class EndpointSpec:
    """由 ``IOPConfig`` 生成两端请求参数。

    ``token_request()`` / ``custinfo_request()`` 的输出与旧调用点逐字一致
    （第 1 步已用本地假 IOP server 比对过 url / headers / body 字节，含
    ``&`` / ``=`` 等特殊字符凭据的 urlencode 行为）。
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
