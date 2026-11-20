# -*- coding: utf-8 -*-
"""FastAPI 集成适配（可选）：计数中间件与 GET 查询路由工厂。

``fastapi`` 在函数内延迟导入，保证 ``import call_stat`` 零第三方依赖；
纯库 / CLI 场景不需要装 fastapi。lifespan 的 startup / flush 仍由宿主
``main.py`` 编排（见根目录 README 的「职责边界」），本模块不接管应用生命周期。
"""
from __future__ import annotations

from typing import Optional

from . import query as _q
from .counter import get_config, record_call


async def count_call_middleware(request, call_next):
    """每次访问服务，当日调用量在内存 +1（无论请求成功或失败都计数）。

    累计到 ``flush_threshold`` 次才由 counter 写一次 SQLite。计数放在
    ``finally``，因此 GET 查询自身也被计数，但本次返回的 callCount 不含本次请求。
    """
    try:
        response = await call_next(request)
    finally:
        record_call()
    return response


def build_router():
    """按包内 ``.env`` 的 ``CALL_STAT_QUERY_PATH`` 注册 GET 查询路由。

    用法::

        app.include_router(call_stat.build_router())

    ``date`` / ``month`` 必须且只能提供一个；单日与月查询都读到完整累计
    （含尚未落库的部分），月查询按 ``calendar.monthrange`` 补齐整月每一天。
    """
    from fastapi import APIRouter

    cfg = get_config()
    router = APIRouter()

    @router.get(cfg.query_path)
    async def call_stat_query(date: Optional[str] = None,
                              month: Optional[str] = None):
        ok, desc = _q.validate_params(date, month)
        if not ok:
            return _q.error_response(desc)
        if date is not None:
            return {"res_code": 0, "res_desc": "", "result": _q.date_result(date)}
        return {"res_code": 0, "res_desc": "", "result": _q.month_result(month)}

    return router
