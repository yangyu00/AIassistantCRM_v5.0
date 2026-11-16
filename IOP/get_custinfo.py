from config import ImmutableYamlConfig
from src.log_util import my_log
from IOP.get_token import TOKENS, token2global, client_timeout
import aiohttp
import asyncio
import datetime
import time

# 配置在 import 时读一次即可：ImmutableYamlConfig 是单例且返回只读配置，
# 避免每次请求都重新打开并解析 config.yaml。
global_config = ImmutableYamlConfig()


async def get_custom_info_in_global(tel):
    if TOKENS.access_token is None or datetime.datetime.now() > TOKENS.deadline:
        await token2global()
        for _ in range(3):
            if TOKENS.access_token is None or datetime.datetime.now() > TOKENS.deadline:
                await token2global()
            else:
                break

    if TOKENS.access_token is None or datetime.datetime.now() > TOKENS.deadline:
        # 统一降级为空 dict：返回 None 不会触发 main.py 的 except，会一路传到
        # ProductInfoRaw 并抛 TypeError，反而让最该降级的场景返回 500。
        my_log.error("IOP token 不可用：刷新后仍无有效 access_token，本次标签查询降级为空标签")
        return {}

    # 上游接口约定：access_token 必须放在 URL 查询串中（不接受 Authorization 头），
    # 且只提供 http 明文端点。均为上游实际要求，不可改动。
    url = ("http://{host}:{port}/openapi/httpService/UserService"
           "?access_token={access_token}&method=qryCustInfoByChannel&format=json").format(
        host=global_config['IOP']['CustInfo']['host'],
        port=global_config['IOP']['CustInfo']['port'],
        access_token=TOKENS.access_token)

    # 上游强制要求 Content-Type 声明 charset=gbk 并显式携带 Accept-Encoding，
    # 即使与实际序列化编码不一致也需照发，不可改动。
    headers = {'Content-Type': 'application/json; charset=gbk', 'Accept-Encoding': 'gzip, deflate'}

    post_data = {
        "VERSION": f"{global_config['IOP']['CustInfo']['version']}",
        "TELNUM": tel,
        "TYPE": f"{global_config['IOP']['CustInfo']['type']}",
    }

    start = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=client_timeout()) as client:
            async with client.post(url, json=post_data, headers=headers) as response:
                status_code = response.status
                try:
                    # 上游返回的 Content-Type 不规范，沿用原有约定不做类型校验
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
