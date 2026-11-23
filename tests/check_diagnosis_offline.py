"""诊断离线自检：一次性跑完「注册 → 解析 → 入库 → 取数 → 拼装 → 路由」。"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 写入侧 load_profiles() 读包内 .env；按当前约定 .env 允许随仓库入库、本仓库已随附，
# 仅当检出里确实缺失时才临时从 .env.example 复制一份，脚本结束删除。
# 在线侧 services 读自己的 services/.env（不存在时用 db_config 的代码内默认值），
# 不依赖这份文件。
ENV = ROOT / "sftp_avl2db" / ".env"
_created_env = not ENV.is_file()
if _created_env:
    shutil.copyfile(ENV.with_name(".env.example"), ENV)

_log_dir = ROOT / "log"
_log_existed = _log_dir.exists()

from services import db_config, dimensions, reader     # noqa: E402
from services.product_info_raw import (                  # noqa: E402
    AuxiliaryPortrait, ProductInfoBase, ProductInfoRaw)
from services.loss_control import (                      # noqa: E402
    ProductInfoByLossControl, parse_custom_tags)
from services.opportunity_recall import (                # noqa: E402
    ProductInfoByOpportunityRecall)
from sftp_avl2db.avl_specs import (                     # noqa: E402
    AVL_SPECS, shard_index, table_name, validate_specs)
from sftp_avl2db.config import load_profiles            # noqa: E402
from sftp_avl2db.parsers import PARSER_KINDS, build_parser   # noqa: E402
from sftp_avl2db.sink.router import build_layout        # noqa: E402
from sftp_avl2db.sink.schema import TableSchema         # noqa: E402
import services.diagnosis.logic as logic                # noqa: E402
from services.diagnosis import (                        # noqa: E402
    ProductInfoByDiagnosis, build_diagnosis_record,
)
from services.selling_point_script import (             # noqa: E402
    SELLING_POINT_SECTIONS, ProductInfoBySellingPoint,
    build_selling_point_object, extract_selling_point_fields,
    parse_tag_script, pick_script_text,
)
from dataclasses import replace                          # noqa: E402

SAMPLES = ROOT / "io_samples"
TMP = Path(tempfile.mkdtemp(prefix="diagnosis_check_"))

# 写入侧配置（建库）与在线侧配置（查询）各自独立装配，指向同一批临时库文件：
# 两侧代码 / 配置互不引用，只在磁盘上的 db 文件交汇。
write_profiles = load_profiles(runtime_dir=str(TMP))
validate_specs(write_profiles)
read_config = db_config.load(
    avl_db_root=TMP / "sqlite_db",
    dimensions_db=TMP / "dimensions.db",
)
reader.configure(read_config)
db_config.validate(read_config)

DIAG_AVLS = [n for n, s in AVL_SPECS.items() if s.project == "diagnosis"]

PID = "202611200000000001"          # 业务键自洽的产品ID
BT = "套餐焕新融宽直点"              # F09628 样例已有的 BUSINESS_TYPE
TEL = "15054675009"                 # F09633 样例已有的手机号
BIZ_NAME = "【五福套餐】流量放心用，月费58元"

FALLBACK_PID = "202611210000000099"       # F09629 无此 ID → 诊断不适用 → 卖点话术
NO_INTERSECT_PID = "202611210000000098"   # 有映射但场景无交集 → 诊断不适用 → 卖点话术
TEL_NO_INTERSECT = "15000000099"          # F09633 里只有不相干场景
MATCH_IDS = ("SCENE_LABEL_0", "SCENE_LABEL_1", "SCENE_LABEL_2")
TAG_TEXTS = (
    "切入话术\n基于客户标签特征切入（零号）：切入A\n拒绝挽留话术\n价格高：挽留A",
    "切入话术\n基于客户标签特征切入（壹号）：切入B\n拒绝挽留话术\n**担心合约**：挽留B",
    "切入话术\n基于客户标签特征切入（贰号）：切入C\n拒绝挽留话术\n流量少：挽留C",
)

_failures = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f' → {detail}' if detail else ""))
    if not ok:
        _failures.append(label)


def build_avl(avl, date):
    """用注册的表结构 + 声明的解析器把样例 AVL 写进临时库。"""
    conf = write_profiles[avl]
    schema = TableSchema.from_profile(conf)
    layout = build_layout(conf)
    db = conf.db_dir / f"{avl}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        for _, tbl in layout.all_targets():
            conn.execute(schema.create_table_sql(tbl))
        parser = build_parser(conf)
        rows = list(parser.parse(SAMPLES / f"{avl}{date}000000.AVL"))
        for row in rows:
            _, tbl = layout.target(avl, row)
            conn.execute(schema.insert_sql(tbl), row)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def append_rows(avl, rows):
    conf = write_profiles[avl]
    schema = TableSchema.from_profile(conf)
    layout = build_layout(conf)
    db = conf.db_dir / f"{avl}.db"
    conn = sqlite3.connect(str(db))
    try:
        for row in rows:
            _, tbl = layout.target(avl, row)
            conn.execute(schema.insert_sql(tbl), row)
        conn.commit()
    finally:
        conn.close()


def main():
    print("=== 1. 表注册 / 解析 / 入库 ===")
    for avl in DIAG_AVLS:
        n = build_avl(avl, "20261120")
        check(f"{avl} 样例入库", n == 10, f"{n} 行")
    build_avl("F09554", "20261110")

    print("=== 1b. 单一 flat 解析器回归（商机与诊断共用） ===")
    check("解析器只有 flat / continuation",
          PARSER_KINDS == ("flat", "continuation"), str(PARSER_KINDS))
    check("12 个 AVL 的 parser 全部合法",
          all(s.parser in PARSER_KINDS for s in AVL_SPECS.values()))
    old_p = build_parser(write_profiles["F09554"])
    check("商机用 FlatColumnParser / 12 列 / 主键第 3 列",
          type(old_p).__name__ == "FlatColumnParser" and
          old_p.column_count == 12 and old_p.primary_key_indexes == (2,),
          f'{type(old_p).__name__} cols={old_p.column_count} pk={old_p.primary_key_indexes}')
    for avl, expect in [("F09627", (1,)), ("F09628", (1, 2)),
                        ("F09629", (2,)), ("F09633", (2, 3)),
                        ("F09635", (2,))]:
        p = build_parser(write_profiles[avl])
        check(f"{avl} 主键下标按注册换算", p.primary_key_indexes == expect,
              f'{p.primary_key_indexes} expect={expect}')
    check("F09635 主键 / 在线查询键均为 tag_name",
          write_profiles["F09635"].primary_key == ("tag_name",) and
          db_config.TABLE_PRIMARY_KEYS["tagid_tagname"] == ("tag_name",) and
          db_config.AVL_DB_SPECS["F09635"].where == ("tag_name",),
          f'write={write_profiles["F09635"].primary_key} '
          f'online={db_config.TABLE_PRIMARY_KEYS["tagid_tagname"]}')
    # 末列原样保留文本：商机 "3" 由 SQLite 亲和性转整数，切入话术保持全文
    qr_first = next(build_parser(write_profiles["F09630"]).parse(
        SAMPLES / "F0963020261120000000.AVL"))
    check("诊断末列原样保留文本（未被转 int）",
          isinstance(qr_first[-1], str) and len(qr_first[-1]) > 20 and
          "电视会员" in qr_first[-1], repr(qr_first[-1][:30]))
    old_first = next(build_parser(write_profiles["F09554"]).parse(
        SAMPLES / "F0955420261110000000.AVL"))
    check("商机末列原样保留文本", old_first[-1] == "3", repr(old_first[-1]))

    print("=== 1c. F09633 分表（千万级用户场景表）===")
    conf33 = write_profiles["F09633"]
    n33 = conf33.num_tables
    check("F09633 声明为分表（写入侧 / 在线侧各自声明）",
          AVL_SPECS["F09633"].sharded is True and
          db_config.AVL_DB_SPECS["F09633"].sharded is True)
    check("在线侧查询键与分表键一致（product_no）",
          db_config.AVL_DB_SPECS["F09633"].where == ("product_no",))
    check("分表数来自两侧各自的声明",
          n33 == 128 and
          read_config.profiles["F09633"].num_tables == 128, str(n33))
    check("两侧分表口径一致（同一算法，各自实现）",
          db_config.shard_index(TEL, n33) == shard_index(TEL, n33))
    expect_tbl = table_name("F09633", shard_index(TEL, n33))
    db33 = conf33.db_dir / "F09633.db"
    conn = sqlite3.connect(str(db33))
    try:
        tbls = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check("建满 128 张分表", len(tbls) == n33, f"{len(tbls)} 张")
        check(f"样例号码落在 {expect_tbl}", expect_tbl in tbls)
        hit = conn.execute(
            f"SELECT COUNT(*) FROM {expect_tbl} WHERE product_no = ?",
            (TEL,)).fetchone()[0]
        check("目标分表内可查到该号码", hit > 0, f"{hit} 行")
        leaked = sum(
            conn.execute(f"SELECT COUNT(*) FROM {t} WHERE product_no = ?",
                         (TEL,)).fetchone()[0]
            for t in tbls if t != expect_tbl)
        check("该号码未散落到其它分表", leaked == 0, f"{leaked} 行")
    finally:
        conn.close()
    rows33 = asyncio.run(reader.query_many(
        "F09633", where={"product_no": TEL}))
    check("reader 按 telnum 精确定位单张分表", bool(rows33), f"{len(rows33)} 行")
    check("分表 AVL 缺 product_no → []（不广播、不抛异常）",
          asyncio.run(reader.query_many(
              "F09633", where={"scene": "超套"})) == [])

    print("=== 1d. 卖点维表 → 商机标签 ID===")
    dim_db = TMP / "dimensions.db"
    tag_cols = list(db_config.SELLING_POINTS_TAG_COLUMNS[:3])
    conn = sqlite3.connect(str(dim_db))
    try:
        defs = []
        for col, typ in db_config.SELLING_POINTS_SCHEMA:
            not_null = (" NOT NULL" if col in db_config.SELLING_POINTS_PRIMARY_KEY
                        else "")
            defs.append(f"{col} {typ}{not_null}")
        conn.execute(
            f"CREATE TABLE {db_config.SELLING_POINTS_TABLE} ({', '.join(defs)}, "
            f"version INTEGER NOT NULL, "
            f"PRIMARY KEY ({', '.join(db_config.SELLING_POINTS_PRIMARY_KEY)}))")

        def add(pid, name, label_ids, match_result, tags=(), fallback_script=""):
            cols = ["product_id", "product_name", "label_id", "match_result",
                    "fallback_script"]
            cols += tag_cols
            # 不足的「标签N」列补空串，保证绑定数与列数一致
            tag_values = (list(tags) + [""] * len(tag_cols))[:len(tag_cols)]
            vals = [pid, name, label_ids, match_result, fallback_script] + tag_values
            conn.execute(
                f"INSERT INTO {db_config.SELLING_POINTS_TABLE} ({', '.join(cols)}, version) "
                f"VALUES ({', '.join('?' for _ in cols)}, 1)", vals)

        add("202608041615335173", "维表产品-173", "DIM_LABEL_A\nDIM_LABEL_B", "")
        add("202608041615335172", "维表产品-172", "DIM_LABEL_C|DIM_LABEL_A", "")
        add("202608041615335174", "维表产品-174", "", "")   # 空标签 ID → 空画像
        add(PID, BIZ_NAME, "DIM_LABEL_A", "\n".join(MATCH_IDS[:2]),
            TAG_TEXTS[:2])
        add(FALLBACK_PID, "【兜底套餐】兜底摘要", "", "\n".join(MATCH_IDS),
            TAG_TEXTS, fallback_script=TAG_TEXTS[1])
        add(NO_INTERSECT_PID, "【交集空套餐】交集空摘要", "",
            "\n".join(MATCH_IDS[:2]), TAG_TEXTS[:2])
        conn.commit()
    finally:
        conn.close()

    dimensions.configure(dim_db)
    sp_opp = dimensions.load_selling_points(
        ["202608041615335173", "202608041615335172",
         "202608041615335174", "NOT_IN_DIM", "202608041615335173"])
    check("只返回维表命中的 3 个 ID", len(sp_opp) == 3, str(sorted(sp_opp)))
    check("换行分隔 → 保序元组",
          sp_opp["202608041615335173"].label_ids == ("DIM_LABEL_A", "DIM_LABEL_B"),
          str(sp_opp["202608041615335173"].label_ids))
    check("竖线分隔 + 去重保序",
          sp_opp["202608041615335172"].label_ids == ("DIM_LABEL_C", "DIM_LABEL_A"),
          str(sp_opp["202608041615335172"].label_ids))
    check("空标签 ID → 空元组",
          sp_opp["202608041615335174"].label_ids == (),
          str(sp_opp["202608041615335174"].label_ids))

    # 全量映射：后续诊断取数与在线路由都复用这一份
    sp = dimensions.load_selling_points([
        "202608041615335173", "202608041615335172", "202608041615335174",
        PID, FALLBACK_PID, NO_INTERSECT_PID])
    check("维表共命中 6 个产品 ID", len(sp) == 6, str(sorted(sp)))
    check("库里只存原始文本：「匹配结果2」原文保留",
          sp[FALLBACK_PID].match_result == "\n".join(MATCH_IDS),
          sp[FALLBACK_PID].match_result)
    check("库里只存原始文本：tag_* 为「标签N」单元格原文",
          len(sp[FALLBACK_PID].tag_texts) >= 3 and
          TAG_TEXTS[1] in sp[FALLBACK_PID].tag_texts,
          str(len(sp[FALLBACK_PID].tag_texts)))

    iop_stub = {
        "DIM_LABEL_A": ("维度标签A", "值A"),
        "DIM_LABEL_B": ("维度标签B", "值B"),
        "DIM_LABEL_C": ("维度标签C", "值C"),
        # 下面三行模拟 IOP 返回的 ITEMCODE：与 F09635 补的 tag_id 一一对应
        "c8fbbddc4337da705ee054ad4946f905": ("客运个性化运营标签34", "值34"),
        "28f7d17f23ba86e761a47d0182982272": ("客运个性化运营标签82", "值82"),
        "3de853e341b4a5f4e4e5cbc6e882530": ("互联网融合标签94", "值94"),
    }
    opp_obj = ProductInfoByOpportunityRecall(
        "202608041615335173", iop_stub, sp_opp, record={})
    check("商机 queried_label_ids 来自维表",
          opp_obj.queried_label_ids == ["DIM_LABEL_A", "DIM_LABEL_B"],
          str(opp_obj.queried_label_ids))
    check("辅助画像按维表 ID 顺序拼接",
          "维度标签A" in opp_obj.output_script and
          "维度标签B" in opp_obj.output_script and
          opp_obj.output_script.index("维度标签A") <
          opp_obj.output_script.index("维度标签B"),
          opp_obj.auxiliary_portrait)
    check("product_name 也来自维表行",
          opp_obj.selling_point_rows["202608041615335173"].product_name
          == "维表产品-173")
    check("画像以组合方式挂在商机上",
          isinstance(opp_obj, ProductInfoBase) and
          isinstance(opp_obj.portrait, AuxiliaryPortrait) and
          opp_obj.selling_point_rows is sp_opp)
    check("product_selling_point_match_dict 属性不存在",
          not hasattr(opp_obj, "product_selling_point_match_dict"))

    print("=== 1e. 卖点话术模块：原文解析 /「兜底话术」优先 / 四段组合 ===")
    entry, pairs = parse_tag_script(TAG_TEXTS[1])
    check("原文解析「切入话术」", "切入B" in entry, entry)
    check("原文解析「拒绝原因：挽留话术」并去 **",
          pairs == [("担心合约", "挽留B")], str(pairs))
    check("「兜底话术」列优先于「标签N」",
          pick_script_text(sp[FALLBACK_PID]) == TAG_TEXTS[1],
          pick_script_text(sp[FALLBACK_PID])[:40])
    check("「兜底话术」为空时回退第一个非空「标签N」原文",
          pick_script_text(sp[PID]) == TAG_TEXTS[0],
          pick_script_text(sp[PID])[:40])
    rec0 = extract_selling_point_fields(FALLBACK_PID, sp[FALLBACK_PID])
    check("产品名称按【】拆出资费名称 / 摘要",
          rec0["fee_name"] == "兜底套餐" and
          rec0["fee_summary"] == "兜底摘要",
          f'{rec0["fee_name"]} / {rec0["fee_summary"]}')
    check("切入话术来自「兜底话术」原文",
          "切入B" in rec0["entry_script"] and
          "切入A" not in rec0["entry_script"], rec0["entry_script"])
    check("拒绝原因 / 挽留话术来自同一份原文",
          rec0["retain_pairs"] == [("担心合约", "挽留B")],
          str(rec0["retain_pairs"]))
    check("卖点话术固定四段（痛点 / 营销无维表来源）",
          SELLING_POINT_SECTIONS ==
          ("资费名称", "资费摘要", "切入话术", "挽留话术"))
    fb_obj = ProductInfoBySellingPoint(FALLBACK_PID, rec0)
    for seg in SELLING_POINT_SECTIONS:
        check(f"卖点话术输出含【{seg}】", f"【{seg}】" in fb_obj.output_script)
    for seg in ("痛点诊断", "营销话术"):
        check(f"卖点话术输出不含【{seg}】",
              f"【{seg}】" not in fb_obj.output_script)
    check("卖点话术 productName 取维表产品名称原文",
          fb_obj.product_name == "【兜底套餐】兜底摘要", fb_obj.product_name)

    dimensions.configure(TMP / "no_such_dimensions.db")
    check("维表库缺失 → {}（不抛异常）",
          dimensions.load_selling_points(["202608041615335173"]) == {})
    empty_obj = ProductInfoByOpportunityRecall(
        "202608041615335173", iop_stub, {}, record={})
    check("维表未命中 → 标签子集空、画像空",
          empty_obj.queried_label_ids == [] and empty_obj.auxiliary_portrait == "")
    # 还原到有效的临时维表库：第 7 段在线路由会读它，端到端验证「维表 → 画像 / 兜底」。
    # 默认库路径在 __main__ 的 finally 里统一还原，避免污染其它调用方。
    dimensions.configure(dim_db)

    print("=== 1f. 业务包按业务名命名===")
    import services
    from services.loss_control import rewrite as loss_rewrite
    from services.opportunity_recall import rewrite as opp_rewrite
    check("业务包子模块可经 services 访问",
          services.opportunity_recall.__name__.endswith("opportunity_recall") and
          services.loss_control.__name__.endswith("loss_control") and
          services.selling_point_script.__name__.endswith("selling_point_script"))
    check("ProductInfoRewrite 别名不存在",
          not hasattr(opp_rewrite, "ProductInfoRewrite") and
          not hasattr(loss_rewrite, "ProductInfoRewrite"))
    check("包入口显式导出语义化类名",
          services.opportunity_recall.ProductInfoByOpportunityRecall
          is ProductInfoByOpportunityRecall and
          services.loss_control.ProductInfoByLossControl
          is ProductInfoByLossControl and
          services.selling_point_script.ProductInfoBySellingPoint
          is ProductInfoBySellingPoint)

    opp_rec = {
        "content_abstract": "通话摘要", "type_xl": "资费", "cust_demand": "想降费",
        "rec_strategy": "推 19 元套餐", "reject_reason": "太贵",
        "reason_note": "每月多 10 元",
    }
    opp_full = ProductInfoByOpportunityRecall(
        "202608041615335173", {}, {}, opp_rec)
    expected_opp = (
        "<strong>【通话总结】</strong>通话摘要<br>"
        '<strong>【用户需求】</strong><strong><span style="color:blue;">资费：</span></strong>想降费<br>'
        "<strong>【已推荐政策】</strong>推 19 元套餐<br>"
        "<strong>【拒绝原因】</strong><br>"
        '<strong><span style="color:blue;">拒绝原因：</span></strong>太贵<br>'
        '<strong><span style="color:blue;">具体表述：</span></strong>每月多 10 元'
    )
    check("商机四段 HTML 逐字符合预期",
          opp_full.output_script == expected_opp, opp_full.output_script)
    check("商机 product_name 来自映射表",
          opp_full.product_name == "号卡业务回捞商机", str(opp_full.product_name))
    expected_opp_empty = (
        "<strong>【通话总结】</strong>无<br>"
        '<strong>【用户需求】</strong><strong><span style="color:blue;">无：</span></strong>无<br>'
        "<strong>【已推荐政策】</strong>无<br>"
        "<strong>【拒绝原因】</strong><br>"
        '<strong><span style="color:blue;">拒绝原因：</span></strong>无<br>'
        '<strong><span style="color:blue;">具体表述：</span></strong>无'
    )
    check("商机空 record → 仍出四段空记录话术（各段填「无」）",
          ProductInfoByOpportunityRecall(
              "202608041615335173", {}, {}, {}).output_script
          == expected_opp_empty)

    check("控损组合标签 B/I/颜色 + 逆序闭合",
          parse_custom_tags("<$B$I#0070C0>加粗斜体蓝色</> 尾巴") ==
          '<b><i><span style="color: #0070C0">加粗斜体蓝色</span></i></b> 尾巴',
          parse_custom_tags("<$B$I#0070C0>加粗斜体蓝色</> 尾巴"))
    check("控损独立颜色标签",
          parse_custom_tags("A<#FF0000>红") == 'A<span style="color: #FF0000">红')
    loss_obj = ProductInfoByLossControl(
        "202609180955095402", {"strategy_name": "<$B>重点</> 保留"})
    check("控损对象解析 strategy_name",
          loss_obj.output_script == "<b>重点</b> 保留", loss_obj.output_script)
    check("控损无 strategy_name → 「无历史话术记录」",
          ProductInfoByLossControl("x", {}).output_script
          == "无历史话术记录")

    print("=== 1g. 对象依赖按业务收窄（画像只属于商机）===")
    portrait_attrs = ("portrait", "_portrait", "custom_label_info",
                      "selling_point_rows", "queried_label_ids",
                      "queried_label_nvs", "auxiliary_portrait")
    diag_empty = ProductInfoByDiagnosis(PID, {})
    for label, obj in (("控损", loss_obj), ("诊断", diag_empty),
                       ("卖点话术", fb_obj),
                       ("兜底", ProductInfoRaw("x", sp))):
        check(f"{label}不持有画像 / 标签 / 维表 / 实例缓存字段",
              all(not hasattr(obj, attr) for attr in portrait_attrs),
              str([a for a in portrait_attrs if hasattr(obj, a)]))
        check(f"{label}仍继承极简公共基类", isinstance(obj, ProductInfoBase))
    check("卖点话术日志不含画像行",
          "auxiliary_portrait" not in str(fb_obj) and
          "queried_label_ids" not in str(fb_obj))
    check("卖点话术对象不收标签 / 诊断 8 表字段",
          all(not hasattr(fb_obj, a) for a in
              ("custom_label_info", "selling_point_rows", "portrait",
               "business_type", "matched_scenes")))
    check("控损构造只需 (product_id, record)",
          ProductInfoByLossControl("x", {"strategy_name": "A"}).output_script
          == "A")

    print("=== 1h. 业务库未就绪：商机 / 控损空记录、诊断不适用 → 卖点话术 ===")
    from services.routing import (                       # noqa: E402
        BUSINESS_OBJECT_TYPES, build_product_object)
    empty_root = TMP / "no_business_db"
    empty_root.mkdir(parents=True, exist_ok=True)
    reader.configure(db_config.load(
        avl_db_root=empty_root, dimensions_db=dim_db))
    try:
        opp_empty = asyncio.run(build_product_object(
            "202608041615335173", TEL,
            custom_label_info={}, selling_points={}))
        loss_empty = asyncio.run(build_product_object(
            "202609180955095402", TEL,
            custom_label_info={}, selling_points={}))
        check("商机库空 → 仍返回商机对象（code=0 判定成立）",
              isinstance(opp_empty, ProductInfoByOpportunityRecall) and
              isinstance(opp_empty, BUSINESS_OBJECT_TYPES))
        check("商机库空 → 四段空记录话术、不是空串",
              opp_empty.output_script == expected_opp_empty,
              opp_empty.output_script[:80])
        check("控损库空 → 仍返回控损对象，不落诊断 / 兜底",
              isinstance(loss_empty, ProductInfoByLossControl) and
              isinstance(loss_empty, BUSINESS_OBJECT_TYPES))
        check("控损库空 → 空记录话术「无历史话术记录」",
              loss_empty.output_script == "无历史话术记录",
              loss_empty.output_script)
        sp_route = asyncio.run(build_product_object(
            FALLBACK_PID, TEL, custom_label_info={}, selling_points=sp))
        check("业务库未就绪 → 诊断返回 None，路由落到平级卖点话术对象",
              isinstance(sp_route, ProductInfoBySellingPoint) and
              not isinstance(sp_route, ProductInfoByDiagnosis) and
              isinstance(sp_route, BUSINESS_OBJECT_TYPES))
        raw_route = asyncio.run(build_product_object(
            "999999999999999999", TEL,
            custom_label_info={}, selling_points={}))
        check("维表也无该 ID → ProductInfoRaw（code=2）",
              isinstance(raw_route, ProductInfoRaw))
    finally:
        reader.configure(read_config)

    print("=== 2. 补一组业务键自洽的数据 ===")
    append_rows("F09629", [
        ("2026-11-20", BT, PID, BIZ_NAME),
        # 有产品映射，但该号码的用户场景与产品场景无交集 → 5b 验证兜底
        ("2026-11-20", BT, NO_INTERSECT_PID, "【交集空套餐】交集空摘要"),
    ])
    append_rows("F09633", [
        ("2026-11-20", "U_TEST", TEL, "超套|流量使用不断增长", "痛点A|痛点B"),
        ("2026-11-20", "U_NO", TEL_NO_INTERSECT, "完全不相关场景", "痛点X"),
    ])
    append_rows("F09627", [
        ("2026-11-20", "超套", 9),
        ("2026-11-20", "流量使用不断增长", 1),
    ])
    append_rows("F09631", [
        ("2026-11-20", BT, "营销话术A"),
        ("2026-11-20", BT, "营销话术B"),
        ("2026-11-20", BT, "营销话术A"),     # 重复行，验证去重
        ("2026-11-20", BT, "营销话术标签#客运个性化运营标签82&"),
    ])
    append_rows("F09632", [
        ("2026-11-20", BT, "价格贵|流量够用", "挽留话术A|挽留话术B"),
        ("2026-11-20", BT, "标签原因#客运个性化运营标签34&",
         "挽留话术标签#互联网融合标签94&"),
    ])
    # F09635 按标签名反查：tag_id 即 IOP 的 ITEMCODE（主键 = tag_name）
    append_rows("F09635", [
        ("2026-11-20", "c8fbbddc4337da705ee054ad4946f905",
         "客运个性化运营标签34", "", ""),
        ("2026-11-20", "28f7d17f23ba86e761a47d0182982272",
         "客运个性化运营标签82", "", ""),
        ("2026-11-20", "3de853e341b4a5f4e4e5cbc6e882530",
         "互联网融合标签94", "", ""),
    ])
    print("  已补：F09629 / F09633 / F09627 / F09631 / F09632 / F09635")

    print("=== 3. 纯逻辑 ===")
    check("【】拆分", logic.split_fee_name(BIZ_NAME) == ("五福套餐", "流量放心用，月费58元"),
          str(logic.split_fee_name(BIZ_NAME)))
    check("无括号 → 摘要全文", logic.split_fee_name("日照中高端临界用户") ==
          ("", "日照中高端临界用户"))
    check("ASCII [] 写法", logic.split_fee_name("[权益]铂金会员（标准版）") ==
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
    rows = asyncio.run(reader.query_in("F09627", "scene",
                                           ["超套", "流量使用不断增长"]))
    check("IN 查多行", len(rows) == 2, f"{len(rows)} 行")
    rows = asyncio.run(reader.query_many(
        "F09631", where={"business_type": BT, "yx_huashu": ["营销话术A"]}))
    check("多键 AND（含 IN）", len(rows) == 1, f"{len(rows)} 行")
    check("空 IN 集合 → []",
          asyncio.run(reader.query_in("F09627", "scene", [])) == [])
    check("非法列名 → []（不抛异常）",
          asyncio.run(reader.query_many("F09627", where={"bogus": "x"})) == [])
    # 单条查询回归：用样例里真实存在的手机号
    old_rows = list(build_parser(write_profiles["F09554"]).parse(
        SAMPLES / "F0955420261110000000.AVL"))
    key = old_rows[0][2] if old_rows else ""
    rec = asyncio.run(reader.query("F09554", key))
    check("query() 单条查询回归", bool(rec) and rec["product_no"] == key,
          str(rec.get("product_no")))

    print("=== 5. 诊断取数（8 张表：7 表关联 + F09635 标签反查）===")
    import inspect                                       # noqa: E402
    check("诊断取数不再接收 selling_points 参数",
          "selling_points" not in
          inspect.signature(build_diagnosis_record).parameters)
    record = asyncio.run(build_diagnosis_record(PID, TEL))
    check("命中 F09629 → record", record is not None)
    check("正常链路 record 不含任何兜底标记",
          record is not None and "is_fallback" not in record and
          "fallback_reason" not in record)
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
              ["营销话术A", "营销话术B",
               "营销话术标签#客运个性化运营标签82&"],
              str(record["marketing_scripts"]))
        check("挽留话术多值按位置配对", record["retain_pairs"] ==
              [("价格贵", "挽留话术A"), ("流量够用", "挽留话术B"),
               ("标签原因#客运个性化运营标签34&",
                "挽留话术标签#互联网融合标签94&")],
              str(record["retain_pairs"]))
    check("未命中 F09629 → None（诊断不自行兜底，交由路由层）",
          asyncio.run(build_diagnosis_record("NO_SUCH_ID", TEL)) is None)

    print("=== 5b. 诊断不适用 → 平级卖点话术模块 ===")

    # ① 任一 db 查询返回空（F09629 无 FALLBACK_PID）
    fb_db = asyncio.run(build_diagnosis_record(FALLBACK_PID, TEL))
    check("db 查空 → 诊断返回 None，不再自己拼兜底", fb_db is None)
    sp_db = build_selling_point_object(FALLBACK_PID, sp)
    check("db 查空 → 由平级模块给出卖点话术对象",
          isinstance(sp_db, ProductInfoBySellingPoint))
    if sp_db:
        check("db 查空仍按【】拆维表产品名",
              sp_db.record["fee_name"] == "兜底套餐" and
              sp_db.record["fee_summary"] == "兜底摘要",
              f'{sp_db.record["fee_name"]} / {sp_db.record["fee_summary"]}')
        check("db 查空仍取「兜底话术」原文的切入话术",
              "切入B" in sp_db.record["entry_script"] and
              "切入A" not in sp_db.record["entry_script"],
              sp_db.record["entry_script"][:40])

    # ② 场景交集为空：「兜底话术」列为空 → 回退第一个非空「标签N」原文
    fb_inter = asyncio.run(build_diagnosis_record(
        NO_INTERSECT_PID, TEL_NO_INTERSECT))
    check("场景交集为空 → 诊断返回 None", fb_inter is None)
    sp_inter = build_selling_point_object(NO_INTERSECT_PID, sp)
    check("交集空也按【】拆维表产品名",
          sp_inter is not None and
          sp_inter.record["fee_name"] == "交集空套餐",
          str(sp_inter.record["fee_name"]) if sp_inter else "None")
    check("无「兜底话术」时回退「标签N」原文",
          sp_inter is not None and
          "切入A" in sp_inter.record["entry_script"],
          str(sp_inter.record["entry_script"])[:40] if sp_inter else "None")

    # ③ 话术原文全空：仍输出四段，切入 / 挽留填「无」
    empty_row = replace(sp[FALLBACK_PID], fallback_script="",
                        tag_texts=("",) * len(sp[FALLBACK_PID].tag_texts))
    sp_empty = build_selling_point_object(
        FALLBACK_PID, {FALLBACK_PID: empty_row})
    check("话术原文全空 → 切入 / 挽留为空，不抛异常",
          sp_empty is not None and sp_empty.record["entry_script"] == "" and
          sp_empty.record["retain_pairs"] == [])
    check("无话术时仍输出四段（切入 / 挽留填「无」）",
          sp_empty is not None and
          "<strong>【切入话术】</strong>无" in sp_empty.output_script and
          "<strong>【挽留话术】</strong>无" in sp_empty.output_script,
          sp_empty.output_script if sp_empty else "None")

    # ④ 降级边界：维表无该行 / 产品名称为空 → 模块返回 None → 路由落 code=2
    check("维表无该 product_id → 卖点话术模块返回 None",
          build_selling_point_object("NO_SUCH_ID", sp) is None)
    check("产品名称为空 → 卖点话术模块返回 None",
          build_selling_point_object(
              FALLBACK_PID,
              {FALLBACK_PID: replace(sp[FALLBACK_PID], product_name="")})
          is None)

    # ⑤ 维度隔离：卖点话术四段、诊断六段，且卖点话术不做字段替换
    for seg in SELLING_POINT_SECTIONS:
        check(f"卖点话术 output_script 含【{seg}】",
              sp_db is not None and f"【{seg}】" in sp_db.output_script)
    for seg in ("痛点诊断", "营销话术"):
        check(f"卖点话术 output_script 不含【{seg}】",
              sp_db is not None and f"【{seg}】" not in sp_db.output_script)
    check("正常诊断对象仍是六段",
          all(f"【{seg}】" in ProductInfoByDiagnosis(
              PID, record).output_script
              for seg in ("资费名称", "资费摘要", "痛点诊断", "切入话术",
                          "营销话术", "挽留话术")))
    tagged_row = replace(
        sp[FALLBACK_PID],
        fallback_script="切入话术\n客户#客运个性化运营标签34&专属\n"
                        "拒绝挽留话术\n价格高：挽留#互联网融合标签94&")
    tagged_obj = build_selling_point_object(
        FALLBACK_PID, {FALLBACK_PID: tagged_row})
    check("卖点话术原文里的 #标签名& 原样保留（本业务无待替换字段）",
          tagged_obj is not None and
          "#客运个性化运营标签34&" in tagged_obj.output_script and
          "#互联网融合标签94&" in tagged_obj.output_script,
          tagged_obj.output_script[:160] if tagged_obj else "None")

    print("=== 5c. 话术标签占位符替换：#标签名& → F09635 tag_id → IOP ITEMVALUE ===")
    demo = {
        "entry_script": "超出后#客运个性化运营标签34&收费较高",
        "marketing_scripts": ["办套餐#客运个性化运营标签82&"],
        "retain_pairs": [("标签原因#客运个性化运营标签34&",
                          "不如这个#互联网融合标签94&合适")],
    }
    check("收集三类话术里的标签名（保序去重）",
          logic.collect_tag_names(demo) ==
          ["客运个性化运营标签34", "客运个性化运营标签82", "互联网融合标签94"],
          str(logic.collect_tag_names(demo)))
    check("替换纯函数：查不到值的占位符原样保留",
          logic.replace_tag_placeholders(
              "#客运个性化运营标签34&/#未登记标签&",
              {"客运个性化运营标签34": "值34"}) == "值34/#未登记标签&")
    applied = logic.apply_tag_values(
        demo, {"客运个性化运营标签34": "值34",
               "客运个性化运营标签82": "值82",
               "互联网融合标签94": "值94"})
    check("apply_tag_values 覆盖切入 / 营销 / 挽留（含拒绝原因）",
          applied["entry_script"] == "超出后值34收费较高" and
          applied["marketing_scripts"] == ["办套餐值82"] and
          applied["retain_pairs"] == [("标签原因值34", "不如这个值94合适")],
          str(applied))
    check("apply_tag_values 不改原 record",
          "#客运个性化运营标签34&" in demo["entry_script"])

    tagged = asyncio.run(build_diagnosis_record(
        PID, TEL, custom_label_info=iop_stub))
    check("正常链路：按标签名查 F09635 得 tag_id=ITEMCODE，切入话术换成 ITEMVALUE",
          tagged is not None and "值34" in tagged["entry_script"] and
          "#客运个性化运营标签34&" not in tagged["entry_script"],
          str(tagged["entry_script"])[:80] if tagged else "None")
    check("正常链路：营销话术换成 ITEMVALUE",
          tagged is not None and
          any("值82" in x and "#客运个性化运营标签82&" not in x
              for x in tagged["marketing_scripts"]),
          str(tagged["marketing_scripts"]) if tagged else "None")
    check("正常链路：挽留话术（原因 + 话术）换成 ITEMVALUE",
          tagged is not None and
          any("值34" in reason and "值94" in huashu
              for reason, huashu in tagged["retain_pairs"]),
          str(tagged["retain_pairs"]) if tagged else "None")
    check("IOP 降级 / 未注入标签 → 占位符原样保留，不静默吞掉",
          record is not None and
          "#客运个性化运营标签34&" in record["entry_script"],
          str(record["entry_script"])[:80] if record else "None")

    from services import selling_point_script as sp_pkg   # noqa: E402
    check("标签替换只属于诊断模块：卖点话术包不导出任何替换函数",
          not hasattr(sp_pkg, "collect_tag_names") and
          not hasattr(sp_pkg, "replace_tag_placeholders") and
          not hasattr(sp_pkg, "apply_tag_values"))

    print("=== 6. 业务对象拼装 ===")
    obj = ProductInfoByDiagnosis(PID, record)
    check("productName = business_name 原文", obj.product_name == BIZ_NAME, obj.product_name)
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
        # IOP 返回维表里那几个标签，端到端验证「卖点维表选 ID → IOP 取名值 → 画像」
        iop_calls = []

        async def fake_labels(_telnum):
            iop_calls.append(_telnum)
            return dict(iop_stub)
        main.iop_client.get_customer_labels = fake_labels
        args = main.Args(telnum=TEL, productId=[
            main.ProductIds(id=PID),
            main.ProductIds(id="202608041615335173"),   # 商机回捞
            main.ProductIds(id="202609180955095402"),   # 控损（业务库空 → 空记录话术）
            main.ProductIds(id=FALLBACK_PID),           # db 查空 → 平级卖点话术模块
            main.ProductIds(id="999999999999999999"),   # 未命中 → 兜底
        ])
        resp = asyncio.run(main.marketAssistant(args))
        scripts = resp["result"]["allScripts"]
        check("返回 5 条", len(scripts) == 5, str(len(scripts)))
        check("商机 / 诊断共用请求级标签，整请求仅查 1 次 IOP",
              len(iop_calls) == 1, str(iop_calls))
        by_id = {x["productId"]: x for x in scripts}
        d = by_id.get(PID, {})
        check("诊断 code=0", d.get("code") == "0", d.get("code"))
        check("诊断 productName", d.get("productName") == BIZ_NAME,
              str(d.get("productName")))
        check("诊断 script 非空且含资费名称",
              "【资费名称】" in (d.get("script") or ""))
        check("在线诊断 script 里 #标签名& 已换成 IOP ITEMVALUE",
              "值34" in (d.get("script") or "") and
              "#客运个性化运营标签34&" not in (d.get("script") or ""),
              (d.get("script") or "")[:160])
        check("商机回捞仍 code=0",
              by_id.get("202608041615335173", {}).get("code") == "0")
        opp_script = by_id.get("202608041615335173", {}).get("script") or ""
        check("商机画像端到端来自卖点维表 → IOP",
              "【辅助画像】" in opp_script and "维度标签A" in opp_script
              and "维度标签B" in opp_script,
              opp_script[:120])
        check("诊断 script 不带辅助画像（诊断业务不拼画像）",
              "【辅助画像】" not in (d.get("script") or ""))
        fb_resp = by_id.get(FALLBACK_PID, {})
        fb_script = fb_resp.get("script") or ""
        check("db 查空 → 在线侧 code=0（平级卖点话术，不是 code=2）",
              fb_resp.get("code") == "0", str(fb_resp.get("code")))
        check("卖点话术 productName 取维表产品名称原文",
              fb_resp.get("productName") == "【兜底套餐】兜底摘要",
              str(fb_resp.get("productName")))
        check("卖点话术 script 含「兜底话术」原文的切入话术",
              "切入B" in fb_script and "切入A" not in fb_script,
              fb_script[:120])
        check("卖点话术 script 含四段、不含痛点/营销",
              all(f"【{seg}】" in fb_script for seg in
                  ("资费名称", "资费摘要", "切入话术", "挽留话术")) and
              all(f"【{seg}】" not in fb_script for seg in
                  ("痛点诊断", "营销话术")))
        loss_resp = by_id.get("202609180955095402", {})
        check("控损业务库未就绪 → 在线侧 code=0 + 空记录话术",
              loss_resp.get("code") == "0" and
              loss_resp.get("script") == "无历史话术记录",
              str(loss_resp.get("script")))
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
        dimensions.configure(None)          # 还原默认库路径
        shutil.rmtree(TMP, ignore_errors=True)
        if _created_env:
            ENV.unlink(missing_ok=True)
        if not _log_existed:
            shutil.rmtree(_log_dir, ignore_errors=True)
    sys.exit(rc)
