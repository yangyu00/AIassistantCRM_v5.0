"""卖点话术「输出复现」自检：转好的 ``dimensions.db`` **全量**逐字符对照独立参照。

用法::

    python -m local_excel2db --once            # 先生成维表库（需 openpyxl / loguru）
    python tests/check_legacy_output_parity.py

oracle 是 ``tests/legacy_output_reference.py``：逐行直译用户给的四个历史文件
（``product_selling_point_match.py`` / ``卖点维表.xlsx`` 的 consts 构造形状 /
``product_info_raw.py`` / ``rewrite.py``），与现版实现互不 import，因此可以真正
当独立参照。本脚本只负责：

* 从真实 ``dimensions.db`` 读出全部 ``product_id``（不是手上几行样例）；
* 读 ``label_name_mapping`` 表（对应上一版 ``consts.label_name_mapping``）；
* 对每个 ID 构造「无标签 / 命中首个场景 / 命中末尾场景 / 命中前三个场景」等
  IOP 返回，分别比对 ``output_script`` 与 ``productName``。

覆盖上一版四类输出：辅助画像（含硬编码值转换与显示名映射）、切入话术、
卖点、挽留话术。维表库不存在时打印 SKIP 并返回 0（不阻塞其它自检）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from services import db_config, dimensions                 # noqa: E402
from services.selling_point_script import (                # noqa: E402
    build_selling_point_object,
)
import legacy_output_reference as oracle                  # noqa: E402


def _all_product_ids(db):
    conn = sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute(
            f"SELECT product_id FROM {db_config.SELLING_POINTS_TABLE}")]
    finally:
        conn.close()


def _label_sets(row):
    """一个维表行的若干种 IOP 返回形态（值是 [ITEMNAME, ITEMVALUE]）。"""
    yield "无标签", {}
    mr = str(row.match_result or "").strip()
    if not mr:
        return
    scene_ids = mr.split('\n')
    # 场景标签：用「匹配结果2」里的 ID，值取「是」以命中标签专属话术
    for name, chosen in (("首个场景", scene_ids[:1]),
                         ("末尾场景", scene_ids[-1:]),
                         ("前三个场景", scene_ids[:3])):
        yield name, {k: (f"场景标签{k[:4]}", "是") for k in chosen if k}
    # 辅助画像标签：用「标签ID」列的子集，值带一点内容以触发值转换
    if row.label_ids:
        yield "画像标签", {k: (f"平台标签{k[:4]}", "12.5")
                          for k in row.label_ids[:3] if k}


def main():
    db = db_config.default_dimensions_db()
    if not Path(db).is_file():
        print(f"[SKIP] 维表库不存在：{db}")
        print("       先跑 `python -m local_excel2db --once` 生成后再复验。")
        return 0

    dimensions.configure(db)
    try:
        ids = _all_product_ids(db)
        rows = dimensions.load_selling_points(ids)
        mapping = dimensions.load_label_name_mapping()
        print(f"维表库：{db}")
        print(f"product_id {len(rows)} 个 / 标签显示名映射 {len(mapping)} 条")

        fails, checked = [], 0
        with_portrait = 0
        with_scene_hit = 0
        for pid, row in rows.items():
            pspm, cisd = oracle.build_legacy_consts(pid, row)
            for case, labels in _label_sets(row):
                expect_name, expect = oracle.legacy_output(
                    pid, pspm, cisd, labels, mapping)
                obj = build_selling_point_object(
                    pid, {pid: row}, labels, mapping)
                actual = obj.output_script if obj is not None else None
                actual_name = obj.product_name if obj is not None else None
                checked += 1
                if expect != actual or expect_name != actual_name:
                    fails.append((pid, case, expect, actual,
                                  expect_name, actual_name))
                if "【辅助画像】" in (actual or ""):
                    with_portrait += 1
                if case not in ("无标签", "画像标签") and labels:
                    with_scene_hit += 1

        print(f"对照：{len(rows)} 个 product_id / {checked} 次输出 | "
              f"含辅助画像 {with_portrait} 次 / 标签话术命中用例 {with_scene_hit} 次 "
              f"| 不一致 {len(fails)}")
        for pid, case, expect, actual, ename, aname in fails[:3]:
            print(f"\n  pid={pid} case={case}\n"
                  f"   EXPECT name={ename!r} script={expect[:260]!r}\n"
                  f"   ACTUAL name={aname!r} script={(actual or '')[:260]!r}")
        if fails:
            print("FAILED")
            return 1
        print("ALL PASS · 现版输出与独立参照逐字符一致")
        return 0
    finally:
        dimensions.configure(None)


if __name__ == "__main__":
    sys.exit(main())
