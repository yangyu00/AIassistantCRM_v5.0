"""端到端验证：token 被顶掉后，同样查询在**在线请求路径**上不再丢辅助画像。

验证在线请求路径上标签端点返回「access_token invalid」时，客户端自动
invalidate → 重刷 → 重试一次，话术里的【辅助画像】照常出现。

用法::

    python tests/check_iop_selfheal.py

需要在线侧依赖（fastapi / uvicorn / pydantic / aiohttp / loguru）；缺失时
打印 SKIP 并返回 0，不阻塞其它自检。不发真实 IOP 请求（FakeTransport）。
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import main  # noqa: E402
except Exception as e:
    print(f"[SKIP] 未加载在线侧（{type(e).__name__}: {e}）")
    raise SystemExit(0)

from IOP import IOPClient, IOPConfig, HttpResponse  # noqa: E402
from services import db_config, dimensions  # noqa: E402

PID = "202509111424212412"
TEL = "15020868271"
LABEL_ID = "LBL_SELFHEAL"
_fails = []


def check(label, cond, detail=""):
    ok = bool(cond)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}'
          + (f' → {detail}' if detail else ""))
    if not ok:
        _fails.append(label)


class Log:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(m)

    def error(self, m):
        self.lines.append(m)

    def warning(self, m):
        self.lines.append(m)

    def text(self):
        return "\n".join(self.lines)


class FakeTransport:
    """token 端点永远成功；标签端点按 script 返回，用尽后重复最后一个。"""

    def __init__(self, label_script):
        self.label_script = list(label_script)
        self.token_calls = 0
        self.label_calls = 0

    async def post(self, url, *, data=None, json=None, headers=None):
        if "restOauth2Server" in url:
            self.token_calls += 1
            return HttpResponse(status=200, payload={
                "access_token": f"TOK_{self.token_calls}",
                "expires_in": 600000,
            })
        idx = min(self.label_calls, len(self.label_script) - 1)
        self.label_calls += 1
        return self.label_script[idx]


def label_ok():
    return HttpResponse(status=200, payload={
        "res_code": "0",
        "result": {"BUSIINFO": [
            {"ITEMCODE": LABEL_ID, "ITEMNAME": "近三月流量", "ITEMVALUE": "23.2"},
        ]},
    })


def label_token_invalid():
    return HttpResponse(status=200, payload={
        "res_code": "1001", "res_desc": "access_token invalid"})


def make_dim_db(path):
    conn = sqlite3.connect(path)
    try:
        cols = []
        for col, typ in db_config.SELLING_POINTS_SCHEMA:
            nn = " NOT NULL" if col in db_config.SELLING_POINTS_PRIMARY_KEY else ""
            cols.append(f"{col} {typ}{nn}")
        conn.execute(
            f"CREATE TABLE {db_config.SELLING_POINTS_TABLE} "
            f"({', '.join(cols)}, version INTEGER NOT NULL, "
            f"PRIMARY KEY ({', '.join(db_config.SELLING_POINTS_PRIMARY_KEY)}))")
        # 空映射表：避免 load_label_name_mapping() 打「表不存在」的 ERROR
        conn.execute(
            f"CREATE TABLE {db_config.LABEL_NAME_MAPPING_TABLE} "
            "(label_id TEXT NOT NULL, display_name TEXT, "
            "platform_label_name TEXT, version INTEGER NOT NULL, "
            "PRIMARY KEY (label_id))")
        conn.execute(
            f"INSERT INTO {db_config.SELLING_POINTS_TABLE} "
            "(product_id, product_name, label_id, selling_point, "
            " fallback_script, match_result, version) VALUES (?,?,?,?,?,?,1)",
            (PID, "【自愈验证套餐】摘要", LABEL_ID,
             "1、资费：基础10GB国内通用流量。",
             "切入话术\n推荐语：内容\n拒绝挽留话术\n挽留语：内容", ""))
        conn.commit()
    finally:
        conn.close()


async def one_request(label_script, *, retry_enabled=True):
    """替换在线进程共享 IOPClient，跑一次真实 marketAssistant 编排。"""
    cfg = IOPConfig.load(ROOT / "IOP" / ".env")
    cfg = replace(cfg, auth_failure=replace(
        cfg.auth_failure, retry_enabled=retry_enabled))
    cfg = replace(cfg, retry=replace(cfg.retry, max_attempts=1,
                                     backoff_base=0.0, cooldown=0.0))
    log, tr = Log(), FakeTransport(label_script)
    client = IOPClient(config=cfg, transport=tr, logger=log)
    old_client = main.iop_client
    main.iop_client = client
    try:
        args = main.Args(telnum=TEL, productId=[main.ProductIds(id=PID)])
        resp = await main.marketAssistant(args)
        script = resp["result"]["allScripts"][0]["script"]
        return script, log, tr
    finally:
        main.iop_client = old_client


def main_entry():
    print("=" * 78)
    print("端到端：token 失效后同样查询不再丢【辅助画像】")
    print("=" * 78)
    print("  说明：业务库未就绪时的 WARNING 是既有降级路径，与本验证无关。")
    tmp = Path(tempfile.mkdtemp(prefix="iop_selfheal_"))
    dim_db = tmp / "dimensions.db"
    try:
        make_dim_db(dim_db)
        dimensions.configure(dim_db)

        # ① 正常路径
        script, log, tr = asyncio.run(one_request([label_ok()]))
        check("① 正常查询 → 话术含【辅助画像】与标签值",
              "【辅助画像】" in script and "23.2" in script,
              script[:160])
        check("① 正常查询只查 1 次标签",
              tr.label_calls == 1, str(tr.label_calls))

        # ② token 被另一个进程顶掉：上游返回认证失败（修复前这里会静默丢画像）
        script2, log2, tr2 = asyncio.run(
            one_request([label_token_invalid(), label_ok()]))
        check("② token 失效 → 自动重刷并重试，话术仍有【辅助画像】",
              "【辅助画像】" in script2 and "23.2" in script2,
              script2[:160])
        check("② token 刷新 2 次、标签请求 2 次",
              tr2.token_calls == 2 and tr2.label_calls == 2,
              f"token={tr2.token_calls}, label={tr2.label_calls}")
        check("② 日志留下失效→重刷链路",
              "token 失效" in log2.text()
              and "自动重刷成功" in log2.text(),
              log2.text()[-200:])

        # ③ 对照：关掉自动重刷开关，同样场景应当丢画像（证明修复确实生效）
        script3, log3, tr3 = asyncio.run(
            one_request([label_token_invalid(), label_ok()],
                        retry_enabled=False))
        check("③ 对照组（AUTH_RETRY_ENABLED=0）→ 无【辅助画像】",
              "【辅助画像】" not in script3 and tr3.token_calls == 1,
              script3[:160])
        # 在线侧的降级日志走 my_log（落 log/ERROR_*.log），不在注入的 Log 里；
        # 这里只验证 IOP 侧「识别到认证失败但不重刷」。
        check("③ 对照组 IOP 侧识别认证失败但不重刷",
              "auth_failed=True" in log3.text()
              and "自动重刷" not in log3.text(),
              log3.text()[-200:])
    finally:
        dimensions.configure(None)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 78)
    if _fails:
        print(f"FAILED: {len(_fails)} 项 → {_fails}")
        return 1
    print(f"ALL PASS · 0 项失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_entry())
