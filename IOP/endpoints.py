# -*- coding: utf-8 -*-
"""IOP 两个端点的请求形状。

URL 路径、查询参数名、OAuth2 固定参数与请求头都由上游接口约定，集中在这里
定义，调用点只取构造好的结果。``host`` / ``port`` / ``version`` / ``type`` /
凭据由调用方通过 ``cfg`` 传入，不在此处硬编码。

``token_request()`` 用 ``urlencode`` 构造表单串，凭据含 ``&`` / ``=`` 等字符
时也能正确转义；``custinfo_request()`` 每次返回一份新的 headers dict。
"""
from urllib.parse import urlencode as _urlencode

# 上游仅提供 http 明文端点
_SCHEME = "http"

# 两个端点的路径与固定查询参数
_TOKEN_PATH = "/openapi/restOauth2Server/access_token"
_CUSTINFO_PATH = "/openapi/httpService/UserService"
_CUSTINFO_METHOD = "qryCustInfoByChannel"
_CUSTINFO_FORMAT = "json"

# OAuth2 固定参数
_GRANT_TYPE = "client_credentials"

# 上游强制要求的请求头，即使与实际序列化编码不一致也照发，不可改动
_CUSTINFO_HEADERS = {
    "Content-Type": "application/json; charset=gbk",
    "Accept-Encoding": "gzip, deflate",
}


def token_request(cfg):
    """返回 ``(url, body)``；凭据以表单串提交。"""
    t = cfg["IOP"]["TOKEN"]
    url = f"{_SCHEME}://{t['host']}:{t['port']}{_TOKEN_PATH}"
    body = _urlencode({
        "grant_type": _GRANT_TYPE,
        "client_id": t["id"],
        "client_secret": t["secret"],
    })
    return url, body


def custinfo_request(cfg, tel, access_token):
    """返回 ``(url, headers, post_data)``。``access_token`` 按要求放在查询串。"""
    c = cfg["IOP"]["CustInfo"]
    url = (f"{_SCHEME}://{c['host']}:{c['port']}{_CUSTINFO_PATH}"
           f"?access_token={access_token}"
           f"&method={_CUSTINFO_METHOD}"
           f"&format={_CUSTINFO_FORMAT}")
    post_data = {
        "VERSION": f"{c['version']}",
        "TELNUM": tel,
        "TYPE": f"{c['type']}",
    }
    return url, dict(_CUSTINFO_HEADERS), post_data
