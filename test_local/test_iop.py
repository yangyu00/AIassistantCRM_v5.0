# -*- coding: utf-8 -*-
"""IOP 模块离线自检脚本。

原文件把 token / 标签查询两份请求逻辑完整复制了一份，模块级代码一执行就向
``config.yaml`` 里的真实生产地址发两次请求，还写死了真实手机号（F1）。现在：

* 只复用生产模块 ``IOP.get_custinfo.get_custom_info_in_global()``，
  不再复制 URL 模板、headers、post_data、Tokens 类与有效期换算；
* 所有 HTTP 由本地假响应接住；``IOP.http_session.session_context`` 另加
  硬 guard，任何漏网的真实会话都会抛异常而不是联网；
* 生产模块的配置引用也换成假值，连拼出来的 URL 都不含生产 host / 凭据；
* 用占位手机号与占位 token；
* 只有 ``if __name__ == "__main__"`` 才执行，import 本模块零副作用、
  不发请求、不写日志文件。

运行（项目根目录）：

    python -m test_local.test_iop

也可直接 ``python test_local/test_iop.py``，脚本会自行把项目根目录加入
``sys.path``。本脚本不做联网调试：需要对真实/测试环境发请求时请用 curl
等外部工具，不要改回模块级直接请求。
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ""):
    # 直接以文件路径运行时 sys.path[0] 是 test_local/，补上项目根目录。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --- 假数据（不含任何真实号码、地址或凭据）---------------------------------
FAKE_TEL = "13900000000"                  # 占位手机号，非真实用户
FAKE_HOST = "127.0.0.1"
FAKE_PORT = "0"                           # 即便漏网也无法连到服务
FAKE_TOKEN = "fake_token_for_self_check"

FAKE_LABELS = {
    "res_code": "0",
    "result": {"BUSIINFO": [
        {"ITEMCODE": "L1", "ITEMNAME": "标签1", "ITEMVALUE": "是"},
        {"ITEMCODE": "L2", "ITEMNAME": "标签2", "ITEMVALUE": "0.00"},
    ]},
}

FAKE_CFG = {
    "IOP": {
        "TOKEN": {"host": FAKE_HOST, "port": FAKE_PORT,
                  "id": "fake_id", "secret": "fake_secret"},
        "CustInfo": {"host": FAKE_HOST, "port": FAKE_PORT,
                     "version": "1.0.1", "type": "M"},
    }
}


class _LogCollector:
    """替掉 my_log：既不写出日志文件，又能断言日志内容。"""

    def __init__(self):
        self.records = []

    def _add(self, level, msg):
        self.records.append((level, str(msg)))

    def info(self, msg):
        self._add("INFO", msg)

    def error(self, msg):
        self._add("ERROR", msg)

    def warning(self, msg):
        self._add("WARNING", msg)

    def text(self, level=None):
        return "\n".join(m for lv, m in self.records if level is None or lv == level)


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    """按 URL 区分两个端点，并记录每次调用供末尾核对。"""

    def __init__(self, token_status, token_payload, label_status, label_payload):
        self._token = (token_status, token_payload)
        self._label = (label_status, label_payload)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        status, payload = self._token if "restOauth2Server" in url else self._label
        return _FakeResponse(status, payload)


@asynccontextmanager
async def _guard_session_context():
    """兜底：若生产模块改用模块级引用取会话，立刻失败而不是联网。

    ``get_token.py`` / ``get_custinfo.py`` 是 ``from ... import session_context``，
    各自持有独立引用，所以下面还要单独 patch 那两处；这里护的是以后代码
    改成 ``hs.session_context()`` 的情况。
    """
    raise RuntimeError("离线自检禁止创建真实 ClientSession")
    yield  # pragma: no cover


def _reset_state(gt):
    """清掉生产模块的全局状态，避免上一场景的冷却/代次串到下一场景。"""
    gt.TOKENS.access_token = None
    gt.TOKENS.deadline = None
    gt._REFRESH_GEN = 0
    gt._REFRESH_LOCK = None
    gt._REFRESH_LOCK_LOOP = None
    gt._COOLDOWN_UNTIL = 0.0
    gt._COOLDOWN_WARNED_UNTIL = 0.0


def _run_case(name, token_resp, label_resp, expect_labels, expect_error=None):
    import IOP.get_custinfo as gc
    import IOP.get_token as gt
    import IOP.http_session as hs

    _reset_state(gt)
    logs = _LogCollector()
    session = _FakeSession(*token_resp, *label_resp)

    @asynccontextmanager
    async def fake_ctx():
        yield session

    with ExitStack() as stack:
        stack.enter_context(patch.object(gt, "global_config", FAKE_CFG))
        stack.enter_context(patch.object(gc, "global_config", FAKE_CFG))
        stack.enter_context(patch.object(gt, "session_context", fake_ctx))
        stack.enter_context(patch.object(gc, "session_context", fake_ctx))
        stack.enter_context(patch.object(hs, "session_context", _guard_session_context))
        stack.enter_context(patch.object(gt, "my_log", logs))
        stack.enter_context(patch.object(gc, "my_log", logs))
        stack.enter_context(patch.object(hs, "my_log", logs))
        result = asyncio.run(gc.get_custom_info_in_global(FAKE_TEL))

    # 硬保证：所有请求都被假响应接住，且 URL 里没有生产地址
    assert session.calls, f"{name}：没有捕获到任何 HTTP 调用"
    assert all(u.startswith(f"http://{FAKE_HOST}:{FAKE_PORT}/") for u in session.calls), \
        f"{name}：出现非本地 URL：{session.calls}"

    assert isinstance(result, dict), f"{name}：返回不是 dict"
    assert len(result) == expect_labels, \
        f"{name}：期望 {expect_labels} 个标签，实际 {result}"
    if expect_error:
        assert expect_error in logs.text("ERROR"), \
            f"{name}：ERROR 日志缺少 {expect_error}"
    return result, len(session.calls)


def main():
    cases = [
        ("正常路径",
         (200, {"access_token": FAKE_TOKEN, "expires_in": 600}),
         (200, FAKE_LABELS), 2, None),
        ("D2 短有效期 expires_in=300",
         (200, {"access_token": FAKE_TOKEN, "expires_in": 300}),
         (200, FAKE_LABELS), 2, None),
        ("D3 token 502",
         (502, {"res_code": "1", "res_desc": "busy"}),
         (200, FAKE_LABELS), 0, "status=502 | HTTP 状态码非 200"),
        ("标签查询 502",
         (200, {"access_token": FAKE_TOKEN, "expires_in": 600}),
         (502, FAKE_LABELS), 0, "status=502"),
    ]

    print("IOP 离线自检（全部 HTTP 由本地假响应接住，不产生网络连接）")
    for i, (name, token_resp, label_resp, expect, err_kw) in enumerate(cases, 1):
        try:
            result, calls = _run_case(name, token_resp, label_resp, expect, err_kw)
        except Exception as e:
            print(f"  [{i}/{len(cases)}] {name:<28} → 失败：{type(e).__name__}: {e}")
            raise SystemExit(1)
        detail = f"{len(result)} 个标签" if result else "降级为 {}"
        print(f"  [{i}/{len(cases)}] {name:<28} → {detail:<12} "
              f"| HTTP 调用 {calls} 次（均被拦截）")
    print(f"离线自检通过：{len(cases)}/{len(cases)}。"
          f"未使用真实用户号码，未连接任何外部主机。")


if __name__ == "__main__":
    main()
