"""排查工具：抓一次 IOP token 端点**原始响应**，确认有效期字段与单位。

用途（本次问题的关键一步）：本地把 ``expires_in`` 当成「秒」处理，并用
``IOP_LIFETIME_MAX_EXPIRES_IN`` 截断。若上游字段名不是 ``expires_in``、
值带单位、是毫秒、或者是绝对时间戳，代码会静默回落 600s（240s 有效期）
或误判出超长有效期。这个脚本把原始回答摆出来，不猜。

用法::

    python tests/check_iop_token_raw.py
    python tests/check_iop_token_raw.py /path/to/IOP/.env

只读 .env + 发一次真实 token 请求，不改任何数据。凭据不会打印。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = ROOT / "IOP" / ".env"

# 与 IOP/endpoints.py 的契约保持一致（诊断工具独立实现，避免包根 import aiohttp）
_TOKEN_PATH = "/openapi/restOauth2Server/access_token"
_DEFAULTS = {
    "IOP_TOKEN_HOST": "134.80.220.55",
    "IOP_TOKEN_PORT": "80",
    "IOP_TOKEN_ID": "202408131154527921000059",
    "IOP_TOKEN_SECRET": "8bd777c80673af7833105f5bf4219a84",
    "IOP_TOKEN_TIMEOUT": "3",
    "IOP_EXPIRES_IN_FIELDS": "expires_in,expiresIn,expire_in",
    "IOP_LIFETIME_MAX_EXPIRES_IN": "604800",
    "IOP_LIFETIME_DEFAULT_EXPIRES_IN": "600",
    "IOP_LIFETIME_REFRESH_BUFFER": "360",
    "IOP_LIFETIME_MIN_LIFETIME": "60",
}


def read_env(path: Path) -> dict:
    values = dict(_DEFAULTS)
    if not path.is_file():
        print(f"[WARN] 配置文件不存在：{path}，全部按代码内置默认值")
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = (p.strip() for p in line.split("=", 1))
        if k and v and not (v.startswith("<") and v.endswith(">")):
            values[k] = v
    return values


def redact(text: str) -> str:
    """把响应里的 access_token 值打码，避免凭据进日志。"""
    try:
        data = json.loads(text)
    except Exception:
        return text[:2000]
    if isinstance(data, dict):
        for key in ("access_token", "refresh_token", "client_secret"):
            if key in data and data[key]:
                data[key] = f"<redacted:{len(str(data[key]))} chars>"
    return json.dumps(data, ensure_ascii=False, indent=2)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    env = read_env(Path(args[0]) if args else DEFAULT_ENV)

    url = (f"http://{env['IOP_TOKEN_HOST']}:{env['IOP_TOKEN_PORT']}"
           f"{_TOKEN_PATH}")
    body = urlencode({
        "grant_type": "client_credentials",
        "client_id": env["IOP_TOKEN_ID"],
        "client_secret": env["IOP_TOKEN_SECRET"],
    })
    fields = [x.strip() for x in env["IOP_EXPIRES_IN_FIELDS"].split(",")
              if x.strip()]

    print("=" * 78)
    print("IOP token 端点原始响应排查")
    print("=" * 78)
    print(f"  配置文件 : {Path(args[0]) if args else DEFAULT_ENV}")
    print(f"  请求 URL : {url}")
    print(f"  client_id: {env['IOP_TOKEN_ID']}")
    print(f"  有效期字段候选: {fields}")
    print(f"  MAX_EXPIRES_IN={env['IOP_LIFETIME_MAX_EXPIRES_IN']}s "
          f"DEFAULT={env['IOP_LIFETIME_DEFAULT_EXPIRES_IN']}s "
          f"BUFFER={env['IOP_LIFETIME_REFRESH_BUFFER']}s")

    try:
        import requests
    except ImportError:
        print("\n[ERROR] 需要 requests：python -m pip install requests")
        return 2

    try:
        resp = requests.post(url, data=body,
                             timeout=float(env.get("IOP_TOKEN_TIMEOUT", "3")))
    except Exception as e:
        print(f"\n[ERROR] token 请求失败：{type(e).__name__}: {e}")
        print("  → 认证服务不可达 / 超时；此时在线侧会走重试退避 + 冷却降级")
        return 2

    print(f"\n  HTTP status: {resp.status_code}")
    print("  原始响应（access_token 已打码）：")
    print("  " + redact(resp.text).replace("\n", "\n  "))

    if resp.status_code != 200:
        print("\n[结论] 非 200：token 刷新会失败，在线侧降级为空标签。")
        return 1

    try:
        data = resp.json()
    except Exception as e:
        print(f"\n[结论] 响应不是合法 JSON（{e}）：token 刷新会失败。")
        return 1
    if not isinstance(data, dict):
        print(f"\n[结论] 响应顶层是 {type(data).__name__} 而非 dict：刷新会失败。")
        return 1
    if not data.get("access_token"):
        print("\n[结论] 无 access_token：刷新会失败。")
        return 1

    print(f"\n  响应顶层键: {sorted(data.keys())}")
    found = [f for f in fields if f in data]
    print(f"  命中的有效期字段: {found or '无'}")

    if not found:
        print("\n[结论] 没找到任何候选有效期字段！")
        print("  → 在线侧会记 ERROR「缺少有效期字段」并回落 "
              f"DEFAULT={env['IOP_LIFETIME_DEFAULT_EXPIRES_IN']}s "
              f"→ 实际 ttl≈{float(env['IOP_LIFETIME_DEFAULT_EXPIRES_IN']) - float(env['IOP_LIFETIME_REFRESH_BUFFER']):.0f}s")
        print("  → 修复办法：把上游真实字段名加进 IOP_EXPIRES_IN_FIELDS，重启进程。")
        return 1

    raw = data[found[0]]
    print(f"  {found[0]} = {raw!r} (type={type(raw).__name__})")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        print("\n[结论] 值不是纯数字（可能带单位）→ 会回落 DEFAULT，")
        print("       并检查 IOP/.env 注释里是不是写成了 '600000s' 一类形式。")
        return 1

    maxv = float(env["IOP_LIFETIME_MAX_EXPIRES_IN"])
    buf = float(env["IOP_LIFETIME_REFRESH_BUFFER"])
    minv = float(env["IOP_LIFETIME_MIN_LIFETIME"])
    eff = min(v, maxv)
    ttl = min(max(eff - buf, minv), eff)
    print(f"\n  本地换算（按「秒」理解）:")
    print(f"    v={v:.0f}s ({v / 86400:.2f} 天) → 夹到 max={maxv:.0f}s "
          f"→ eff={eff:.0f}s → ttl={ttl:.0f}s ({ttl / 3600:.2f}h)")
    if v > maxv:
        print(f"    [注意] 上游值超过 MAX_EXPIRES_IN，会被截断（差 "
              f"{(v - maxv) / 86400:.2f} 天）")
    if v > 1e9:
        print("    [警告] 这个值更像**绝对时间戳**（Unix 秒），不是剩余秒数！")
        print("           在线侧已打 ERROR 提示；请与上游确认单位。")
    elif 1e5 <= v <= 1e8 and v % 1000 == 0 and 60 <= v / 1000 <= 86400:
        print("    [注意] 这个值也能解释为**毫秒**（/1000 后落在常见有效期区间），")
        print("           请与上游确认单位；代码不做自动换算，避免猜错。")

    print("\n  建议核对:")
    print("    1. 字段名是否在 IOP_EXPIRES_IN_FIELDS 里；")
    print("    2. 单位是秒、毫秒还是绝对时间戳；")
    print("    3. MAX_EXPIRES_IN 是否覆盖上游实际值；")
    print("    4. 两个部署目录的 IOP/.env 凭据是否相同（相同 client_id 会互相顶 token）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
