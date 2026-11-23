"""FastAPI 集成适配（可选）：计数中间件与 GET 查询路由工厂。"""
from __future__ import annotations

from typing import Optional

from . import query as _q
from .counter import get_config, record_call


async def count_call_middleware(request, call_next):
    """每次访问服务，当日调用量在内存 +1（无论请求成功或失败都计数）。"""
    try:
        response = await call_next(request)
    finally:
        record_call()
    return response


def build_router():
    """按包内 ``.env`` 的 ``CALL_STAT_QUERY_PATH`` 注册 GET 查询路由。"""
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
