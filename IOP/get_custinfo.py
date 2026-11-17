from src.log_util import my_log
from IOP.get_token import TOKENS, ensure_token, token_valid
from IOP.http_session import global_config, session_context
from IOP.endpoints import custinfo_request
import aiohttp
import asyncio
import time


async def get_custom_info_in_global(tel):
    await ensure_token()

    if not token_valid():
        # 统一降级为空 dict：返回 None 不会触发 main.py 的 except，会一路传到
        # ProductInfoRaw 并抛 TypeError，反而让最该降级的场景返回 500。
        my_log.error("IOP token 不可用：刷新后仍无有效 access_token，本次标签查询降级为空标签")
        return {}

    # 上游接口约定：access_token 必须放在 URL 查询串中（不接受 Authorization 头），
    # 且只提供 http 明文端点。均为上游实际要求，不可改动。
    # 上游强制要求 Content-Type 声明 charset=gbk 并显式携带 Accept-Encoding，
    # 即使与实际序列化编码不一致也需照发，不可改动。
    url, headers, post_data = custinfo_request(global_config, tel, TOKENS.access_token)

    start = time.perf_counter()
    # 复用应用级共享 session（lifespan 创建/关闭）。
    # session 已带 ClientTimeout；连接/读超时与网络异常就地收口，
    # 不让异常穿透到 main.py（那样 IOP 侧会没有日志）。
    try:
        async with session_context() as client:
            async with client.post(url, json=post_data, headers=headers) as response:
                status_code = response.status
                try:
                    # 上游返回的 Content-Type 不规范，不做类型校验
                    res_data = await response.json(content_type=None)
                except Exception as e:
                    my_log.error(f"IOP 标签查询响应解析失败 | status={status_code} | error={e}")
                    return {}
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        cost_ms = (time.perf_counter() - start) * 1000
        my_log.error(
            f"IOP 标签查询网络失败 | error={type(e).__name__}: {e} | cost={cost_ms:.0f}ms"
        )
        return {}

    cost_ms = (time.perf_counter() - start) * 1000

    if status_code != 200:
        my_log.error(f"IOP 标签查询失败 | status={status_code} | cost={cost_ms:.0f}ms")
        return {}

    if not isinstance(res_data, dict):
        my_log.error(
            f"IOP 标签查询响应结构异常 | status={status_code} | "
            f"type={type(res_data).__name__} | cost={cost_ms:.0f}ms"
        )
        return {}

    # 上游可能返回字符串 "0" 或数字 0，统一按字符串比较
    res_code = str(res_data.get("res_code"))
    if res_code != "0":
        my_log.error(
            f"IOP 标签查询返回失败 | status={status_code} | res_code={res_code} | "
            f"res_desc={res_data.get('res_desc', '')} | cost={cost_ms:.0f}ms"
        )
        return {}

    result = res_data.get("result")
    busi_info = result.get("BUSIINFO") if isinstance(result, dict) else None
    if not isinstance(busi_info, list):
        my_log.error(
            f"IOP 标签查询缺少 BUSIINFO | status={status_code} | "
            f"type={type(busi_info).__name__} | cost={cost_ms:.0f}ms"
        )
        return {}

    custom_label_dict = {}
    for custom_label in busi_info:
        if not isinstance(custom_label, dict):
            continue
        item_code = custom_label.get("ITEMCODE")
        if item_code is None:
            continue
        custom_label_dict.update({item_code: [custom_label.get("ITEMNAME"),
                                              custom_label.get("ITEMVALUE")]})

    my_log.info(
        f"IOP 标签查询成功 | status={status_code} | "
        f"busi_info={len(busi_info)} | labels={len(custom_label_dict)} | cost={cost_ms:.0f}ms"
    )
    return custom_label_dict
