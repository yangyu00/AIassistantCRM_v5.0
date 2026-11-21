# -*- coding: utf-8 -*-
"""诊断新线离线自检：一次性跑完「注册 → 解析 → 入库 → 取数 → 拼装 → 路由」。

不是 pytest 单元测试，是手工跑一次、打印断言结论的冒烟脚本；**不连 SFTP、
不连 IOP、不写包内 runtime/**，所有库落在临时目录，跑完自动清理。

主体只用标准库 + 包内纯模块；末尾的「在线路由」段需要 fastapi / pydantic /
loguru / aiohttp 等运行时依赖，缺失会自动跳过。

    python tests/check_diagnosis_offline.py

用 io_samples 的 8 个诊断 AVL 样例建库后，再补一组业务键自洽的数据
（样例各表业务键彼此不对齐，直接跑只能看到降级），验证 6 个输出字段：
括号拆分、多值按位置配对、场景交集汇总、RN 优先级、多行话术、挽留配对。
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# data_query 在 import 时按 __file__ 读包内 .env；CI / 干净检出里没有 .env，
# 临时从模板复制一份，脚本结束删除（.gitignore 已忽略 .env）。
ENV = ROOT / "sftp_avl2db" / ".env"
_created_env = not ENV.is_file()
if _created_env:
    shutil.copyfile(ENV.with_name(".env.example"), ENV)

_log_dir = ROOT / "log"
_log_existed = _log_dir.exists()

from sftp_avl2db import data_query                       # noqa: E402
from sftp_avl2db.avl_specs import AVL_SPECS, validate_specs   # noqa: E402
from sftp_avl2db.config import load_profiles            # noqa: E402
from sftp_avl2db.parsers import build_parser            # noqa: E402
from sftp_avl2db.sink.schema import TableSchema         # noqa: E402
import services.diagnosis.logic as logic                # noqa: E402
from services.diagnosis import (                        # noqa: E402
    ProductInfoByDiagnosis, build_diagnosis_record,
)

SAMPLES = ROOT / "io_samples"
TMP = Path(tempfile.mkdtemp(prefix="diagnosis_check_"))

# 在线侧与写入侧同源：把所有查询隔离到临时 runtime，不碰包内 runtime/。
data_query.PROFILES = load_profiles(runtime_dir=str(TMP))
validate_specs(data_query.PROFILES)

DIAG_AVLS = [n for n, s in AVL_SPECS.items() if s.project == "diagnosis"]

PID = "202611200000000001"          # 新增的自洽产品ID
BT = "套餐焕新融宽直点"              # F09628 样例已有的 BUSINESS_TYPE
TEL = "15054675009"                 # F09633 样例已有的手机号
BIZ_NAME = "【五福套餐】流量放心用，月费58元"

_failures = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f' → {detail}' if detail else ""))
    if not ok:
        _failures.append(label)


def build_avl(avl, date):
    """用注册的表结构 + 声明的解析器把样例 AVL 写进临时库。"""
    conf = data_query.PROFILES[avl]
    schema = TableSchema.from_profile(conf)
    db = conf.db_dir / f"{avl}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(schema.create_table_sql(avl))
        parser = build_parser(conf)
        rows = list(parser.parse(SAMPLES / f"{avl}{date}000000.AVL"))
        conn.executemany(schema.insert_sql(avl), rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def append_rows(avl, rows):
    conf = data_query.PROFILES[avl]
    schema = TableSchema.from_profile(conf)
    db = conf.db_dir / f"{avl}.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(schema.insert_sql(avl), rows)
        conn.commit()
    finally:
        conn.close()


def main():
    print("=== 1. 表注册 / 解析 / 入库 ===")
    for avl in DIAG_AVLS:
        n = build_avl(avl, "20261120")
        check(f"{avl} 样例入库", n == 10, f"{n} 行")
    build_avl("F09554", "20261110")       # 旧业务回归用

    print("=== 2. 补一组业务键自洽的数据 ===")
    append_rows("F09629", [
        ("2026-11-20", BT, PID, BIZ_NAME),
    ])
    append_rows("F09633", [
        ("2026-11-20", "U_TEST", TEL, "超套|流量使用不断增长", "痛点A|痛点B"),
    ])
    append_rows("F09627", [
        ("2026-11-20", "超套", 9),
        ("2026-11-20", "流量使用不断增长", 1),
    ])
    append_rows("F09631", [
        ("2026-11-20", BT, "营销话术A"),
        ("2026-11-20", BT, "营销话术B"),
        ("2026-11-20", BT, "营销话术A"),     # 重复行，验证去重
    ])
    append_rows("F09632", [
        ("2026-11-20", BT, "价格贵|流量够用", "挽留话术A|挽留话术B"),
    ])
    print("  已补：F09629 / F09633 / F09627 / F09631 / F09632")

    print("=== 3. 纯逻辑 ===")
    check("【】拆分", logic.split_fee_name(BIZ_NAME) == ("五福套餐", "流量放心用，月费58元"),
          str(logic.split_fee_name(BIZ_NAME)))
    check("无括号 → 摘要全文", logic.split_fee_name("日照中高端临界用户") ==
          ("", "日照中高端临界用户"))
    check("ASCII [] 兼容", logic.split_fee_name("[权益]铂金会员（标准版）") ==
          ("权益", "铂金会员（标准版）"))
    check("多值按位置配对（不等长补空）",
          logic.pairs_by_position("a|b|c", "x|y") ==
          [("a", "x"), ("b", "y"), ("c", "")])
    check("交集保留全部 REASON",
          logic.intersect_scenes([("s1", "r1"), ("s2", "r2"), ("s3", "r3")],
                                ["s1", "s2"]) ==
          (["s1", "s2"], ["r1", "r2"]))
    ordered, ranked = logic.order_scenes_by_priority(
        ["超套", "流量使用不断增长"],
        [{"scene": "超套", "rn": 9}, {"scene": "流量使用不断增长", "rn": 1}])
    check("RN 小者优先", ordered == ["流量使用不断增长", "超套"], str(ordered))
    check("优先级集合", ranked == {"超套", "流量使用不断增长"})

    print("=== 4. 多行 / 多条件查询 ===")
    rows = asyncio.run(data_query.query_in("F09627", "scene",
                                           ["超套", "流量使用不断增长"]))
    check("IN 查多行", len(rows) == 2, f"{len(rows)} 行")
    rows = asyncio.run(data_query.query_many(
        "F09631", where={"business_type": BT, "yx_huashu": ["营销话术A"]}))
    check("多键 AND（含 IN）", len(rows) == 1, f"{len(rows)} 行")
    check("空 IN 集合 → []",
          asyncio.run(data_query.query_in("F09627", "scene", [])) == [])
    check("非法列名 → []（不抛异常）",
          asyncio.run(data_query.query_many("F09627", where={"bogus": "x"})) == [])
    # 旧接口回归：用样例里真实存在的手机号
    old_rows = list(build_parser(data_query.PROFILES["F09554"]).parse(
        SAMPLES / "F0955420261110000000.AVL"))
    key = old_rows[0][2] if old_rows else ""
    rec = asyncio.run(data_query.query("F09554", key))
    check("query() 单条旧接口回归", bool(rec) and rec["product_no"] == key,
          str(rec.get("product_no")))

    print("=== 5. 诊断取数（7 张表关联） ===")
    record = asyncio.run(build_diagnosis_record(PID, TEL))
    check("命中 F09629 → record", record is not None)
    if record:
        check("资费名称来自 PRODUCT_BUSINESS 的 name【】内",
              record["fee_name"] == "五福套餐", record["fee_name"])
        check("资费摘要来自 name【】外",
              record["fee_summary"] == "流量放心用，月费58元", record["fee_summary"])
        check("BUSINESS_TYPE 关联键", record["business_type"] == BT,
              record["business_type"])
        check("交集场景", record["matched_scenes"] == ["超套", "流量使用不断增长"],
              str(record["matched_scenes"]))
        check("痛点诊断汇总全部交集场景", len(record["pain_points"]) == 3,
              str(record["pain_points"]))
        check("切入话术按最高优先级场景取",
              record["top_scene"] == "流量使用不断增长" and
              record["top_scene_source"] == "rank" and bool(record["entry_script"]),
              f'{record["top_scene"]} / {record["top_scene_source"]}')
        check("营销话术多行且去重", record["marketing_scripts"] ==
              ["营销话术A", "营销话术B"], str(record["marketing_scripts"]))
        check("挽留话术多值按位置配对", record["retain_pairs"] ==
              [("价格贵", "挽留话术A"), ("流量够用", "挽留话术B")],
              str(record["retain_pairs"]))
    check("未命中 F09629 → None",
          asyncio.run(build_diagnosis_record("NO_SUCH_ID", TEL)) is None)

    print("=== 6. 业务对象拼装 ===")
    obj = ProductInfoByDiagnosis(PID, {}, {}, record, None)
    check("productName = 资费名称", obj.product_name == "五福套餐", obj.product_name)
    for label in ("资费名称", "资费摘要", "痛点诊断", "切入话术", "营销话术", "挽留话术"):
        check(f"output_script 含【{label}】", f"【{label}】" in obj.output_script)
    check("output_script 含实际话术内容", "营销话术A" in obj.output_script and
          "挽留话术B" in obj.output_script)
    print("  --- output_script 预览 ---")
    print("  " + obj.output_script.replace("<br>", "\n  "))

    print("=== 7. 在线路由（main.py，需运行时依赖） ===")
    try:
        import main
    except Exception as e:
        print(f"  [SKIP] 未加载在线侧（{type(e).__name__}: {e}）")
    else:
        async def fake_labels(_telnum):
            return {}
        main.iop_client.get_customer_labels = fake_labels
        args = main.Args(telnum=TEL, productId=[
            main.ProductIds(id=PID),
            main.ProductIds(id="202608041615335173"),   # 商机回捞
            main.ProductIds(id="999999999999999999"),   # 未命中 → 兜底
        ])
        resp = asyncio.run(main.marketAssistant(args))
        scripts = resp["result"]["allScripts"]
        check("返回 3 条", len(scripts) == 3, str(len(scripts)))
        by_id = {x["productId"]: x for x in scripts}
        d = by_id.get(PID, {})
        check("诊断 code=0", d.get("code") == "0", d.get("code"))
        check("诊断 productName", d.get("productName") == "五福套餐",
              str(d.get("productName")))
        check("诊断 script 非空且含资费名称",
              "【资费名称】" in (d.get("script") or ""))
        check("商机回捞仍 code=0",
              by_id.get("202608041615335173", {}).get("code") == "0")
        check("未知ID 仍 code=2",
              by_id.get("999999999999999999", {}).get("code") == "2")

    print("\n=== 结论 ===")
    if _failures:
        print(f"FAILED: {len(_failures)} 项 → {_failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
        if _created_env:
            ENV.unlink(missing_ok=True)
        if not _log_existed:
            shutil.rmtree(_log_dir, ignore_errors=True)
    sys.exit(rc)
