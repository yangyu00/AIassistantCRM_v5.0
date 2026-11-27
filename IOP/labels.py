"""标签响应的逐层校验、BUSIINFO 组装与降级原因分类。"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 降级状态码：get_customer_labels_with_status() 的第二个返回值。
# ---------------------------------------------------------------------------
STATUS_OK = "ok"                              # 查询成功且至少 1 项标签
STATUS_OK_EMPTY = "ok_empty"                  # 查询成功但号码无标签
STATUS_TOKEN_UNAVAILABLE = "token_unavailable"  # token 不可用 / 冷却期短路
STATUS_NETWORK_ERROR = "network_error"        # 建连 / 读超时 / 网络异常
STATUS_PARSE_ERROR = "parse_error"            # 响应体 JSON 解析失败
STATUS_HTTP_ERROR = "http_error"              # 标签端点 HTTP 非 200
STATUS_BAD_STRUCTURE = "bad_structure"       # 响应体不是 dict
STATUS_BUSINESS_ERROR = "business_error"      # res_code != "0"
STATUS_MISSING_BUSIINFO = "missing_busiinfo"  # result.BUSIINFO 缺失 / 非 list


def _blob(*parts) -> str:
    return " ".join(str(p) for p in parts if p is not None).lower()


def is_auth_failure(status_code, res_data, auth_cfg):
    """判断标签查询失败是否属于「token 失效」，返回 ``(是否, 命中依据)``。

    HTTP 401/403、业务码在配置白名单、或错误描述里出现 token / 失效 / 过期
    等关键词，任一命中即视为认证失败。``auth_cfg=None`` 时不做识别（兼容
    旧调用方）。判定为认证失败后由 ``IOPClient`` invalidate + 重刷 + 重试一次。
    """
    if auth_cfg is None:
        return False, ""
    if status_code in (401, 403):
        return True, f"http_status={status_code}"
    if not isinstance(res_data, dict):
        return False, ""

    res_code = str(res_data.get("res_code", "")).strip()
    if res_code and res_code != "0" and res_code in auth_cfg.codes:
        return True, f"res_code={res_code}"

    # 只看错误描述类字段，不把 result / BUSIINFO 里的业务文本当认证错误，
    # 避免标签内容里出现「失效」等词时被误判成 token 失效。
    text = _blob(res_data.get("res_desc"), res_data.get("error"),
                 res_data.get("error_description"), res_data.get("msg"),
                 res_data.get("message"))
    for kw in auth_cfg.keywords:
        if kw and kw in text:
            return True, f"keyword={kw}"
    return False, ""


def parse_custinfo_response_ex(
    res_data: Any,
    *,
    status_code: int,
    logger: Any,
    cost_ms: float,
    auth_cfg: Any = None,
) -> tuple[dict, str, bool]:
    """逐层校验并组装标签，返回 ``(labels, status, auth_failed)``。"""
    if status_code != 200:
        auth, why = is_auth_failure(status_code, res_data, auth_cfg)
        logger.error(
            f"IOP 标签查询失败 | status={status_code} | cost={cost_ms:.0f}ms | "
            f"reason={STATUS_HTTP_ERROR} | auth_failed={auth} | {why}")
        return {}, STATUS_HTTP_ERROR, auth

    if not isinstance(res_data, dict):
        logger.error(
            f"IOP 标签查询响应结构异常 | status={status_code} | "
            f"type={type(res_data).__name__} | cost={cost_ms:.0f}ms | "
            f"reason={STATUS_BAD_STRUCTURE}")
        return {}, STATUS_BAD_STRUCTURE, False

    # 上游可能返回字符串 "0" 或数字 0，统一按字符串比较
    res_code = str(res_data.get("res_code"))
    if res_code != "0":
        auth, why = is_auth_failure(status_code, res_data, auth_cfg)
        logger.error(
            f"IOP 标签查询返回失败 | status={status_code} | res_code={res_code} | "
            f"res_desc={res_data.get('res_desc', '')} | cost={cost_ms:.0f}ms | "
            f"reason={STATUS_BUSINESS_ERROR} | auth_failed={auth} | {why}")
        return {}, STATUS_BUSINESS_ERROR, auth

    result = res_data.get("result")
    busi_info = result.get("BUSIINFO") if isinstance(result, dict) else None
    if not isinstance(busi_info, list):
        auth, why = is_auth_failure(status_code, res_data, auth_cfg)
        logger.error(
            f"IOP 标签查询缺少 BUSIINFO | status={status_code} | "
            f"type={type(busi_info).__name__} | cost={cost_ms:.0f}ms | "
            f"reason={STATUS_MISSING_BUSIINFO} | auth_failed={auth} | {why}")
        return {}, STATUS_MISSING_BUSIINFO, auth

    custom_label_dict = {}
    for custom_label in busi_info:
        if not isinstance(custom_label, dict):
            continue
        item_code = custom_label.get("ITEMCODE")
        if item_code is None:
            continue
        custom_label_dict[item_code] = [custom_label.get("ITEMNAME"),
                                        custom_label.get("ITEMVALUE")]

    status = STATUS_OK if custom_label_dict else STATUS_OK_EMPTY
    logger.info(
        f"IOP 标签查询成功 | status={status_code} | "
        f"busi_info={len(busi_info)} | labels={len(custom_label_dict)} | "
        f"cost={cost_ms:.0f}ms | reason={status}")
    return custom_label_dict, status, False


def parse_custinfo_response(
    res_data: Any,
    *,
    status_code: int,
    logger: Any,
    cost_ms: float,
) -> dict:
    """原入口：只返回 ``{ITEMCODE: [ITEMNAME, ITEMVALUE]}``。

    需要区分降级原因时用 ``parse_custinfo_response_ex()``。
    """
    labels, _, _ = parse_custinfo_response_ex(
        res_data, status_code=status_code, logger=logger, cost_ms=cost_ms)
    return labels
