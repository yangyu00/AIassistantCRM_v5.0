# -*- coding: utf-8 -*-
"""``IOP.http_session`` —— 第 4 步：薄适配层（原 121 行实现已迁出）。

原文件负责：读项目根 ``config.yaml`` 的 ``ImmutableYamlConfig`` 单例、构造
``client_timeout()``、维护模块级共享 ``ClientSession``（创建 / 关闭 / 降级）。

现在这些职责分别落在：

===========================  ================================================
原内容                        现在
===========================  ================================================
``global_config`` 单例        ``IOP/config.py`` 模块内常量（不读外部文件）
``client_timeout()``          ``IOPConfig.timeout`` + ``AiohttpTransport``
共享 session 与生命周期       ``AiohttpTransport`` 实例字段（``IOP/transport.py``）
loguru 日志                   构造时注入；默认 ``NullLogger`` 静默
===========================  ================================================

本文件**只做转发**，没有第二套实现。``main.py`` 的
``from IOP.http_session import init_session, close_session`` 与
``test_local/test_iop.py`` 的同名 import 保持有效，一行未改。

默认实例由 ``IOP/_compat.py`` 懒创建，用模块内配置、不注入 logger。
"""
from ._compat import (
    client_timeout,
    close_session,
    config_view,
    get_session,
    init_session,
    session_context,
)

# 旧符号保留：形状与 config.yaml 的 IOP 段一致，但值来自 IOP/config.py，不读盘。
# 详见 _compat.config_view() 文档里关于「不再是整份项目配置单例」的说明。
global_config = config_view()

__all__ = [
    "global_config",
    "client_timeout",
    "init_session",
    "close_session",
    "get_session",
    "session_context",
]
