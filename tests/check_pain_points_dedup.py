"""F09633 上游违约兜底自检：诊断主链路 ``pain_points`` 按首次出现去重。

不打任何服务、不连 SFTP / IOP。用内存桩替换 ``diagnosis.query.reader`` 的
查询，跑真实 ``build_diagnosis_record()``，断言：

1. 正常三条不同 REASON 全部保留（回归「汇总全部交集场景」）；
2. 上游全量 AVL 偶发重复 ``(product_no, scene)`` 时，两行同文案只输出一条；
3. 去重按首次出现顺序，不误伤不同文案；
4. 纯逻辑层 ``intersect_scenes()`` 本身仍不去重，去重只发生在主链路。

用法：``python3 tests/check_pain_points_dedup.py``
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.diagnosis import query as dq                     # noqa: E402
from services.diagnosis import logic                          # noqa: E402
from services.diagnosis.constants import (                    # noqa: E402
    AVL_CONF_USER_SCENE, AVL_PRODUCT_BUSINESS, AVL_PRODUCT_SCENE,
    AVL_PRODUCT_WLHUASHU, AVL_PRODUCT_YXHUASHU, AVL_SCENE_QRHUASHU,
    AVL_SCENE_RANK, AVL_TAGID_TAGNAME,
)

PID = "202611200000000001"
TEL = "15054675009"
BT = "套餐焕新"

_failures = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' +
          (f" → {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def make_stub(user_rows):
    """7 表关联的内存桩；话术文本不含 #标签名&，finalize 不会查 F09635。"""
    async def query_many(avl, *, where, limit=None):
        if avl == AVL_PRODUCT_BUSINESS:
            return [{"business_name": "【五福套餐】流量放心用",
                     "business_type": BT}]
        if avl == AVL_PRODUCT_SCENE:
            return [{"scene": "超套"}, {"scene": "流量不够用"}]
        if avl == AVL_CONF_USER_SCENE:
            return [dict(r) for r in user_rows]
        if avl == AVL_PRODUCT_YXHUASHU:
            return [{"yx_huashu": "营销话术A"}]
        if avl == AVL_PRODUCT_WLHUASHU:
            return [{"reject_reason": "价格贵", "wl_huashu": "挽留话术A"}]
        if avl == AVL_TAGID_TAGNAME:
            return []
        return []

    async def query_in(avl, column, values, *, limit=None):
        if avl == AVL_SCENE_RANK:
            return [{"scene": "超套", "rn": 1},
                    {"scene": "流量不够用", "rn": 2}]
        if avl == AVL_SCENE_QRHUASHU:
            return [{"scene": s, "qr_huashu": "切入话术"} for s in values]
        return []

    return query_many, query_in


def run(user_rows):
    qm, qi = make_stub(user_rows)
    with patch.object(dq.reader, "query_many", qm), \
         patch.object(dq.reader, "query_in", qi):
        return asyncio.run(dq.build_diagnosis_record(PID, TEL))


def main():
    print("=== 1. 正常数据：全部不同 REASON 仍全部召回 ===")
    normal = [
        {"scene": "超套", "reason": "痛点A"},
        {"scene": "流量不够用", "reason": "痛点B"},
        {"scene": "超套", "reason": "痛点C"},       # 同场景、不同文案
    ]
    rec = run(normal)
    check("诊断 record 构建成功", rec is not None)
    check("交集场景按首次出现顺序",
          rec["matched_scenes"] == ["超套", "流量不够用"],
          str(rec["matched_scenes"]))
    check("三条不同痛点文案全部保留",
          rec["pain_points"] == ["痛点A", "痛点B", "痛点C"],
          str(rec["pain_points"]))

    print("=== 2. 上游违约：同 (product_no, scene) 两行同文案 ===")
    duplicated = [
        {"scene": "超套", "reason": "痛点X"},
        {"scene": "超套", "reason": "痛点X"},       # 写入侧不做去重时的形态
        {"scene": "流量不够用", "reason": "痛点Y"},
    ]
    rec2 = run(duplicated)
    check("诊断 record 仍构建成功（不报错、不降级）", rec2 is not None)
    check("痛点文案按首次出现去重，不输出重复",
          rec2["pain_points"] == ["痛点X", "痛点Y"],
          str(rec2["pain_points"]))
    check("user_scenes 仍保留原始召回形态（未改写输入）",
          rec2["user_scenes"] == ["超套", "超套", "流量不够用"],
          str(rec2["user_scenes"]))
    check("matched_scenes 不受影响",
          rec2["matched_scenes"] == ["超套", "流量不够用"],
          str(rec2["matched_scenes"]))

    print("=== 3. 去重边界：纯逻辑层不去重 ===")
    check("intersect_scenes 仍保留同场景的多条 REASON",
          logic.intersect_scenes(
              [("超套", "痛点X"), ("超套", "痛点X")], ["超套"]) ==
          (["超套"], ["痛点X", "痛点X"]))
    check("空文案不占位",
          logic.dedup(["痛点X", "", "痛点X", "痛点Y"]) ==
          ["痛点X", "痛点Y"])

    print("=== 结论 ===")
    if _failures:
        print("FAILED: %d 项 → %s" % (len(_failures), _failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
