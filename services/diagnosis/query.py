"""诊断取数：按「产品ID + 手机号」关联 8 张维表，产出结构化 record。

本模块只认诊断自己的 8 张表，不读卖点维表、不做维表兜底：7 表关联任一
环节失败、场景交集为空或六项输出全空时一律返回 ``None``，由
``services/routing.py`` 继续询问与诊断平级的卖点话术模块。

三类话术（切入 / 营销 / 挽留）里的 ``#标签名&`` 在此统一替换：先按标签名查
第 8 张表 F09635（主键 tag_name）拿到 tag_id，它即 IOP 返回的 ITEMCODE，
再用该 ITEMCODE 对应的 ITEMVALUE 替换占位符。本模块不 import IOP，标签由
编排层按请求注入；任何一环缺数据时占位符原样保留，不静默吞掉。
"""
from __future__ import annotations

from services import reader

from .constants import (
    AVL_CONF_USER_SCENE,
    AVL_PRODUCT_BUSINESS,
    AVL_PRODUCT_SCENE,
    AVL_PRODUCT_WLHUASHU,
    AVL_PRODUCT_YXHUASHU,
    AVL_SCENE_QRHUASHU,
    AVL_SCENE_RANK,
    AVL_TAGID_TAGNAME,
)
from .logic import (
    apply_tag_values,
    collect_tag_names,
    dedup,
    expand_retain_pairs,
    expand_user_scenes,
    extract_scripts,
    intersect_scenes,
    order_scenes_by_priority,
    split_fee_name,
)

# 诊断链路不适用时的原因（只写日志，不带进任何业务对象）
MISS_REASON_DB_EMPTY = "db_empty"
MISS_REASON_EMPTY_INTERSECTION = "empty_intersection"
MISS_REASON_EMPTY_OUTPUT = "empty_output"


def _miss(reason):
    """诊断不适用：留一行排查日志，返回 None 让路由层继续往下走。"""
    print(f"WARNING:: 诊断未命中（{reason}），交由卖点话术模块处理", flush=True)
    return None


async def _load_tag_values(tag_names, custom_label_info):
    """标签名 → tag_id（F09635）→ ITEMCODE（IOP）→ ITEMVALUE。

    F09635 以 ``tag_name`` 为主键：话术里出现的是标签名，必须按名字反查。
    IOP 结果形如 ``{ITEMCODE: [ITEMNAME, ITEMVALUE]}``；查不到标签名、查不到
    ITEMCODE 或 ITEMVALUE 为 None 时都不写入映射，占位符保持原样。
    """
    if not tag_names or not custom_label_info:
        return {}
    rows = await reader.query_many(
        AVL_TAGID_TAGNAME, where={"tag_name": list(tag_names)})
    name_to_id = {}
    for row in rows:
        name = str(row.get("tag_name") or "").strip()
        tag_id = str(row.get("tag_id") or "").strip()
        if name and tag_id:
            name_to_id[name] = tag_id

    values = {}
    for name, tag_id in name_to_id.items():
        item = custom_label_info.get(tag_id)
        if item is None:
            item = custom_label_info.get(str(tag_id))
        if isinstance(item, dict):
            value = item.get("ITEMVALUE")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            value = item[1]          # [ITEMNAME, ITEMVALUE]
        else:
            value = None
        if value is not None:
            values[name] = value
    return values


async def build_diagnosis_record(product_id, telnum, *, custom_label_info=None):
    """只按诊断 8 表组装 record，并把话术里的标签占位符换成 IOP 值。

    命中且六项输出有内容 → record dict；任一环节为空 → ``None``。诊断不消费
    ``selling_points``，是否改用卖点维表话术完全由 ``routing`` 决定。
    """
    labels = custom_label_info or {}

    async def finalize(record):
        """成功 record 的统一出口：#标签名& → ITEMVALUE。"""
        if not record:
            return record
        tag_names = collect_tag_names(record)
        if not tag_names:
            return record
        tag_values = await _load_tag_values(tag_names, labels)
        return apply_tag_values(record, tag_values)

    def db_empty(avl):
        return _miss(f"{MISS_REASON_DB_EMPTY}:{avl}")

    # ① 产品ID → 策略(BUSINESS_NAME) / 产品(BUSINESS_TYPE)
    business_rows = await reader.query_many(
        AVL_PRODUCT_BUSINESS, where={"product_id": product_id}, limit=1)
    if not business_rows:
        return db_empty(AVL_PRODUCT_BUSINESS)

    business = business_rows[0]
    business_name = str(business.get("business_name") or "").strip()
    business_type = str(business.get("business_type") or "").strip()

    fee_name, fee_summary = split_fee_name(business_name)

    # ② 产品场景（F09628，多行）
    product_scene_rows = await reader.query_many(
        AVL_PRODUCT_SCENE, where={"business_type": business_type})
    product_scenes = dedup(
        [str(row.get("scene") or "").strip() for row in product_scene_rows])
    if not product_scene_rows or not product_scenes:
        return db_empty(AVL_PRODUCT_SCENE)

    # ③ 用户场景 + 痛点诊断（F09633，多行；单元格多值按位置配对）
    # F09633 千万级行、按 product_no 分 128 张表；这里带的标量 product_no
    # 既是查询键也是分表键，reader 会精确定位到一张分表，不广播。
    user_scene_rows = await reader.query_many(
        AVL_CONF_USER_SCENE, where={"product_no": telnum})
    user_pairs = expand_user_scenes(user_scene_rows)
    if not user_scene_rows or not user_pairs:
        return db_empty(AVL_CONF_USER_SCENE)

    # ④ 交集：痛点诊断保留全部交集场景的 REASON；交集为空即诊断不适用
    matched_scenes, pain_points = intersect_scenes(user_pairs, product_scenes)
    if not matched_scenes:
        return _miss(MISS_REASON_EMPTY_INTERSECTION)

    # ⑤ 切入话术：交集场景按 RN 排优先级，取第一个能查到话术的场景
    rank_rows = await reader.query_in(
        AVL_SCENE_RANK, "scene", matched_scenes)
    if not rank_rows:
        return db_empty(AVL_SCENE_RANK)
    ordered_scenes, ranked_set = order_scenes_by_priority(
        matched_scenes, rank_rows)
    qr_rows = await reader.query_in(
        AVL_SCENE_QRHUASHU, "scene", ordered_scenes)
    if not qr_rows:
        return db_empty(AVL_SCENE_QRHUASHU)
    qr_map = {
        str(row.get("scene") or "").strip(): str(row.get("qr_huashu") or "").strip()
        for row in qr_rows if row.get("qr_huashu")
    }
    top_scene = next((scene for scene in ordered_scenes if qr_map.get(scene)), None)
    entry_script = qr_map.get(top_scene, "") if top_scene else ""
    top_scene_source = (
        "rank" if top_scene and top_scene in ranked_set
        else "fallback" if top_scene else "none"
    )

    # ⑥ 营销话术（F09631，多行）
    yx_rows = await reader.query_many(
        AVL_PRODUCT_YXHUASHU, where={"business_type": business_type})
    marketing_scripts = extract_scripts(yx_rows, "yx_huashu")
    if not yx_rows or not marketing_scripts:
        return db_empty(AVL_PRODUCT_YXHUASHU)

    # ⑦ 挽留话术（F09632，拒绝原因 / 话术各自拆分后按位置配对）
    wl_rows = await reader.query_many(
        AVL_PRODUCT_WLHUASHU, where={"business_type": business_type})
    retain_pairs = expand_retain_pairs(wl_rows)
    if not wl_rows or not retain_pairs:
        return db_empty(AVL_PRODUCT_WLHUASHU)

    record = {
        "product_id": product_id,
        "telnum": telnum,
        "business_name": business_name,
        "business_type": business_type,
        "fee_name": fee_name,
        "fee_summary": fee_summary,
        "product_scenes": product_scenes,
        "user_scenes": [scene for scene, _ in user_pairs],
        "matched_scenes": matched_scenes,
        "pain_points": pain_points,
        "top_scene": top_scene,
        "top_scene_source": top_scene_source,
        "entry_script": entry_script,
        "marketing_scripts": marketing_scripts,
        "retain_pairs": retain_pairs,
    }

    # 命中了产品ID 但六个输出字段全空：诊断同样不适用，避免 code=0 + 一屏「无」
    if not any([fee_name, fee_summary, pain_points, entry_script,
                marketing_scripts, retain_pairs]):
        return _miss(MISS_REASON_EMPTY_OUTPUT)
    return await finalize(record)


__all__ = ["build_diagnosis_record"]
