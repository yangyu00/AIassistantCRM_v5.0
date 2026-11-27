"""IOP 离线自检：零第三方依赖、不打真实上级、不连数据库。

覆盖：有效期换算（含别名 / 缺字段 / 疑似时间戳）、
token 失效自动重刷与重试一次、普通业务失败不误重刷、续期失败保留旧
token、冷却期短路可观测、降级状态码、后台续期任务生命周期、向后兼容入口。

用法::

    python tests/check_iop_offline.py
    REPO_ROOT=/path/to/repo python tests/check_iop_offline.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from dataclasses import replace
from pathlib import Path

ROOT = Path(os.environ.get("REPO_ROOT",
                           str(Path(__file__).resolve().parents[1])))
sys.path.insert(0, str(ROOT))

# ---- aiohttp 最小 stub：只为让 IOP 包能 import，不发真实请求 ----
def _has_real_aiohttp():
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


if not _has_real_aiohttp():
    _aio = types.ModuleType("aiohttp")

    class _ClientError(Exception):
        pass

    class _ClientTimeout:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _ClientSession:
        def __init__(self, **kwargs):
            raise RuntimeError("stub: 不应创建真实 session")

    _aio.ClientError = _ClientError
    _aio.ClientTimeout = _ClientTimeout
    _aio.ClientSession = _ClientSession
    sys.modules["aiohttp"] = _aio

from IOP import (  # noqa: E402
    IOPClient, IOPConfig, HttpResponse, TokenManager,
    STATUS_OK, STATUS_OK_EMPTY, STATUS_TOKEN_UNAVAILABLE,
    STATUS_BUSINESS_ERROR, STATUS_HTTP_ERROR,
    is_auth_failure, parse_custinfo_response, parse_custinfo_response_ex,
)

_failures = []


def check(label, cond, detail=""):
    ok = bool(cond)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}'
          + (f' → {detail}' if detail else ""))
    if not ok:
        _failures.append(label)


class Log:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(("INFO", msg))

    def error(self, msg):
        self.lines.append(("ERROR", msg))

    def warning(self, msg):
        self.lines.append(("WARNING", msg))

    def text(self):
        return "\n".join(m for _, m in self.lines)


def token_ok(raw=600000, field="expires_in", name="TOK"):
    d = {"access_token": name}
    d[field] = raw
    return HttpResponse(status=200, payload=d)


def token_bad(status=502):
    return HttpResponse(status=status, payload={"error": "upstream_error"})


def label_ok():
    return HttpResponse(status=200, payload={
        "res_code": "0",
        "result": {"BUSIINFO": [
            {"ITEMCODE": "LBL_A", "ITEMNAME": "近三月流量", "ITEMVALUE": "23.2"},
        ]},
    })


def label_empty():
    return HttpResponse(status=200, payload={
        "res_code": "0", "result": {"BUSIINFO": []}})


def label_auth_http():
    return HttpResponse(status=401, payload={"res_code": "1001",
                                            "res_desc": "access_token invalid"})


def label_auth_business():
    return HttpResponse(status=200, payload={
        "res_code": "1001", "res_desc": "access_token invalid"})


def label_business_busy():
    return HttpResponse(status=200, payload={
        "res_code": "9999", "res_desc": "系统繁忙，请稍后再试"})


class FakeTransport:
    """按脚本顺序返回响应；脚本用尽后重复最后一个。可传入 callable 动态生成。"""

    def __init__(self, token_script, label_script):
        self.token_script = list(token_script)
        self.label_script = list(label_script)
        self.token_calls = 0
        self.label_calls = 0

    @staticmethod
    def _next(script, i):
        item = script[min(i, len(script) - 1)] if script else None
        if callable(item):
            return item(i)
        return item

    async def post(self, url, *, data=None, json=None, headers=None):
        if "restOauth2Server" in url:
            resp = self._next(self.token_script, self.token_calls)
            self.token_calls += 1
            return resp
        resp = self._next(self.label_script, self.label_calls)
        self.label_calls += 1
        return resp


def fast_cfg(**kw):
    """真实 .env 配置的加速版：免退避、短冷却。"""
    c = IOPConfig.load(ROOT / "IOP" / ".env")
    return replace(c, retry=replace(c.retry, max_attempts=1,
                                    backoff_base=0.0, cooldown=0.4))


def make_client(token_script, label_script, **kw):
    log = Log()
    tr = FakeTransport(token_script, label_script)
    c = IOPClient(config=fast_cfg(**kw), transport=tr, logger=log)
    return c, tr, log


# ---------------------------------------------------------------------------
async def part_lifetime():
    print("=== 1. 有效期换算：600000 不是 240s ===")
    cfg = IOPConfig.load(ROOT / "IOP" / ".env")
    p = cfg.lifetime
    tm = TokenManager(config=cfg)
    print(f"  生效: default={p.default_expires_in:.0f}s "
          f"buffer={p.refresh_buffer:.0f}s max={p.max_expires_in:.0f}s "
          f"({p.max_expires_in / 86400:.1f}天)")

    ttl_big, note_big = tm.effective_lifetime(600000)
    check("上游 600000 秒 → ttl=599640s（约 6.94 天），不再被截断到 24h",
          ttl_big == 599640 and note_big is None, f"ttl={ttl_big}, note={note_big}")
    ttl_def, _ = tm.effective_lifetime(p.default_expires_in)
    check("只有回落 default=600 时才是 240s", ttl_def == 240, f"ttl={ttl_def}")
    ttl_bad, note_bad = tm.effective_lifetime("600000s")
    check("非法值回落 240s 且带 note（会打 ERROR）",
          ttl_bad == 240 and "非数字" in note_bad, f"note={note_bad}")
    check("有效期字段名候选含标准名与别名",
          cfg.expires_in_fields[:2] == ("expires_in", "expiresIn"),
          str(cfg.expires_in_fields))

    # 缺字段 / 别名 / 疑似时间戳的日志
    log = Log()
    tr = FakeTransport([token_ok(field="expiresIn", raw=600000)], [])
    c = IOPClient(config=fast_cfg(), transport=tr, logger=log)
    await c.token_manager.ensure_token()
    check("命中别名 expiresIn → 不回落 240s",
          c.token_manager.refresh_due_in() > 590000,
          f"due={c.token_manager.refresh_due_in():.0f}s")
    check("别名命中会记 INFO", "别名" in log.text(), log.text()[:160])

    log2 = Log()
    tr2 = FakeTransport([HttpResponse(status=200,
                                      payload={"access_token": "T",
                                               "token_type": "bearer"})], [])
    c2 = IOPClient(config=fast_cfg(), transport=tr2, logger=log2)
    await c2.token_manager.ensure_token()
    check("缺 expires_in → 回落 240s 且记 ERROR（以前完全静默）",
          "缺少有效期字段" in log2.text()
          and c2.token_manager.refresh_due_in() <= 240,
          log2.text()[:200])

    log3 = Log()
    tr3 = FakeTransport([token_ok(raw=1799999999)], [])
    c3 = IOPClient(config=fast_cfg(), transport=tr3, logger=log3)
    await c3.token_manager.ensure_token()
    check("疑似绝对时间戳/毫秒 → 显式 ERROR 提示核对单位",
          "疑似绝对时间戳" in log3.text(), log3.text()[:200])
    check("刷新成功日志带上游原始值与本地 ttl",
          "raw_expires_in" in log3.text() and "ttl=" in log3.text(),
          log3.text()[-200:])


async def part_auth_retry():
    print("\n=== 2. token 失效：invalidate → 重刷 → 重试一次 ===")
    for name, auth_resp in (("HTTP 401", label_auth_http()),
                            ("业务码 1001 + access_token invalid",
                             label_auth_business())):
        c, tr, log = make_client(
            [token_ok(name="TOK_1"), token_ok(name="TOK_2")],
            [auth_resp, label_ok()])
        labels, status = await c.get_customer_labels_with_status("15020868271")
        check(f"{name} → 自动重刷后拿到标签",
              len(labels) == 1 and status == STATUS_OK,
              f"labels={labels}, status={status}")
        check(f"{name} → token 刷新 2 次、标签请求 2 次",
              tr.token_calls == 2 and tr.label_calls == 2,
              f"token={tr.token_calls}, label={tr.label_calls}")
        check(f"{name} → 日志含失效与重刷链路",
              "token 失效" in log.text() and "自动重刷成功" in log.text(),
              log.text()[-220:])

    # 重刷也失败 → 降级，不无限循环
    c, tr, log = make_client([token_ok(), token_bad()],
                             [label_auth_business(), label_ok()])
    labels, status = await c.get_customer_labels_with_status("15020868271")
    check("重刷也失败 → 明确降级且只重试一次",
          labels == {} and status == STATUS_TOKEN_UNAVAILABLE
          and tr.label_calls == 1, f"status={status}, label_calls={tr.label_calls}")

    # 普通业务失败不得误判为 token 失效
    c, tr, log = make_client([token_ok()], [label_business_busy(), label_ok()])
    labels, status = await c.get_customer_labels_with_status("15020868271")
    check("普通业务失败（系统繁忙）→ 不触发重刷",
          labels == {} and status == STATUS_BUSINESS_ERROR
          and tr.token_calls == 1 and tr.label_calls == 1,
          f"status={status}, token={tr.token_calls}, label={tr.label_calls}")

    # 关闭自动重刷开关
    cfg = fast_cfg()
    cfg = replace(cfg, auth_failure=replace(cfg.auth_failure,
                                            retry_enabled=False))
    log = Log()
    tr = FakeTransport([token_ok()], [label_auth_business()])
    c = IOPClient(config=cfg, transport=tr, logger=log)
    labels, status = await c.get_customer_labels_with_status("15020868271")
    check("AUTH_RETRY_ENABLED=0 → 只记日志不重刷",
          labels == {} and tr.token_calls == 1,
          f"token={tr.token_calls}")


async def part_other_paths():
    print("\n=== 3. 冷却期 / 续期失败 / 降级状态 / 向后兼容 ===")
    # 冷却期短路：0 次额外 HTTP，且日志可见
    c, tr, log = make_client([token_bad()], [label_ok()])
    r1 = await c.get_customer_labels_with_status("15020868271")
    r2 = await c.get_customer_labels_with_status("15020868271")
    check("冷却期两次查询都降级为 token_unavailable",
          r1[1] == STATUS_TOKEN_UNAVAILABLE
          and r2[1] == STATUS_TOKEN_UNAVAILABLE, f"{r1[1]} / {r2[1]}")
    check("冷却期第二次 0 次 HTTP（不打认证、不打标签）",
          tr.label_calls == 0 and tr.token_calls == 1,
          f"token={tr.token_calls}, label={tr.label_calls}")
    check("冷却期日志可见（不再静默）", "冷却期" in log.text(),
          log.text()[-200:])

    # 续期失败保留仍有效的旧 token
    c, tr, log = make_client([token_ok()], [label_ok()])
    await c.token_manager.ensure_token()
    before = c.token_manager.state.access_token
    tr.token_script = [token_bad()]
    tr.token_calls = 0
    refreshed = await c.token_manager.refresh_once()
    check("续期刷新失败 → 返回 False 且保留仍有效的旧 token",
          refreshed is False and c.token_manager.token_valid()
          and c.token_manager.state.access_token == before,
          f"refreshed={refreshed}, valid={c.token_manager.token_valid()}")
    check("保留旧 token 会记 ERROR（可排查）",
          "保留仍有效的旧 token" in log.text(), log.text()[-200:])

    # 成功但号码无标签 → ok_empty，不是降级
    c, tr, log = make_client([token_ok()], [label_empty()])
    labels, status = await c.get_customer_labels_with_status("15020868271")
    check("上游成功但号码无标签 → status=ok_empty（可与降级区分）",
          labels == {} and status == STATUS_OK_EMPTY, status)

    log = Log()
    compat = parse_custinfo_response(
        label_ok().payload, status_code=200, logger=log, cost_ms=1.0)
    check("parse_custinfo_response 旧签名仍返回 dict",
          isinstance(compat, dict) and len(compat) == 1, str(compat))
    ex_labels, ex_status, ex_auth = parse_custinfo_response_ex(
        label_ok().payload, status_code=200, logger=Log(), cost_ms=1.0)
    check("parse_custinfo_response_ex 返回三元组",
          len(ex_labels) == 1 and ex_status == STATUS_OK and not ex_auth)

    # labels 里认证关键词识别
    cfg = fast_cfg()
    auth, why = is_auth_failure(
        200, {"res_code": "1001", "res_desc": "access_token invalid"},
        cfg.auth_failure)
    check("业务码 1001 命中认证失败", auth and "res_code" in why, why)
    auth_kw, why_kw = is_auth_failure(
        200, {"res_code": "8888", "res_desc": "access_token expired"},
        cfg.auth_failure)
    check("res_desc 关键词命中认证失败", auth_kw and "keyword" in why_kw, why_kw)
    auth2, _ = is_auth_failure(
        200, {"res_code": "9999", "res_desc": "系统繁忙"}, cfg.auth_failure)
    check("普通业务错误不误判为认证失败", not auth2)


async def part_lifecycle():
    print("\n=== 4. 启动预热 + 后台续期任务 ===")
    cfg = fast_cfg()
    cfg = replace(cfg, background_refresh=replace(
        cfg.background_refresh, enabled=True, min_interval=0.05,
        lead=0.01, warmup_timeout=1.0))
    log = Log()
    tr = FakeTransport([token_ok()], [label_ok()])
    c = IOPClient(config=cfg, transport=tr, logger=log)
    await c.start()
    check("start() 预热拿到有效 token",
          c.token_manager.token_valid() and tr.token_calls == 1,
          f"token_calls={tr.token_calls}")
    check("后台续期 task 已创建",
          c._background_task is not None
          and not c._background_task.done())
    await asyncio.sleep(0.12)
    check("长有效期下不会疯狂刷新",
          tr.token_calls == 1, f"token_calls={tr.token_calls}")
    await c.aclose()
    check("aclose() 取消后台 task",
          c._background_task is None and "已停止" in log.text(),
          log.text()[-160:])

    cfg2 = replace(cfg, background_refresh=replace(
        cfg.background_refresh, enabled=False))
    c2 = IOPClient(config=cfg2, transport=FakeTransport([token_ok()], []),
                   logger=Log())
    await c2.start()
    check("BACKGROUND_REFRESH_ENABLED=0 不创建 task",
          c2._background_task is None)
    await c2.aclose()


def part_static():
    print("\n=== 5. 在线编排静态检查 ===")
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    check("main.py 注入 logger（不再用 NullLogger）",
          "IOPClient(logger=my_log)" in main)
    check("main.py 使用 with_status 并传递降级原因",
          "get_customer_labels_with_status" in main
          and "degraded_reason" in main)
    check("日志不输出手机号明文",
          "IOP 标签为空 | telnum_len=" in main
          and "本次请求 IOP 降级 | reason=" in main)
    lu = (ROOT / "src" / "log_util.py").read_text(encoding="utf-8")
    check("configure_logging 让 WARNING 也落盘",
          '("INFO", "WARNING")' in lu)
    env = (ROOT / "IOP" / ".env").read_text(encoding="utf-8")
    check(".env 上限提至 604800 并含新键",
          "IOP_LIFETIME_MAX_EXPIRES_IN=604800" in env
          and "IOP_AUTH_RETRY_ENABLED" in env
          and "IOP_BACKGROUND_REFRESH_ENABLED" in env)
    direct = (ROOT / "tests" / "check_iop_direct.py").read_text(encoding="utf-8")
    check("直连自检注入打印 logger",
          "logger=_StdoutLogger()" in direct and "status=" in direct)


async def main():
    print("=" * 78)
    print("IOP 修复离线自检（不打真实上级 / 不连数据库）")
    print("=" * 78)
    await part_lifetime()
    await part_auth_retry()
    await part_other_paths()
    await part_lifecycle()
    part_static()
    print("\n" + "=" * 78)
    if _failures:
        print(f"FAILED: {len(_failures)} 项 → {_failures}")
        return 1
    print(f"ALL PASS · {len(_failures)} 项失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
