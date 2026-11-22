# -*- coding: utf-8 -*-
"""诊断取数：按「产品ID + 手机号」关联 7 张维表，产出结构化 record。"""
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
)
from .fallback import (
    REASON_DB_EMPTY,
    REASON_EMPTY_INTERSECTION,
    REASON_EMPTY_OUTPUT,
    build_fallback_record,
)
from .logic import (
    dedup,
    expand_retain_pairs,
    expand_user_scenes,
    extract_scripts,
    intersect_scenes,
    order_scenes_by_priority,
    split_fee_name,
)


async def build_diagnosis_record(product_id, telnum, *, selling_points=None):
    """组装诊断话术所需的全部字段。"""
    sp = selling_points or {}

    def fallback(reason):
        """统一的兜底出口。"""
        return build_fallback_record(
            product_id, telnum, selling_points=sp, reason=reason)

    def db_empty(avl):
        return fallback(f"{REASON_DB_EMPTY}:{avl}")

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

    # ④ 交集：痛点诊断保留全部交集场景的 REASON；交集为空即兜底
    matched_scenes, pain_points = intersect_scenes(user_pairs, product_scenes)
    if not matched_scenes:
        return fallback(REASON_EMPTY_INTERSECTION)

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

    # 命中了产品ID 但六个输出字段全空：改用卖点维表兜底，避免
    # code=0 + 一屏「无」；维表无该行时 fallback() 返回 None → code=2。
    if not any([fee_name, fee_summary, pain_points, entry_script,
                marketing_scripts, retain_pairs]):
        return fallback(REASON_EMPTY_OUTPUT)
    return record


__all__ = ["build_diagnosis_record"]
