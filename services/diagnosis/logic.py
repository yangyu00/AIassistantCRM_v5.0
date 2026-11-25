"""诊断话术的业务逻辑：零 I/O、零第三方依赖，随机源可注入，便于离线单测。"""
from __future__ import annotations

import random
from itertools import zip_longest

from .constants import (
    ALLOW_ENTRY_SCRIPT_FALLBACK,
    EMPTY_TEXT,
    FEE_BRACKET_RE,
    MULTI_VALUE_SPLIT_RE,
    SMALLER_RN_HIGHER_PRIORITY,
    TAG_PLACEHOLDER_RE,
)


# 拆分与配对
def split_fee_name(business_name):
    """按括号拆资费名称 / 资费摘要，返回 ``(fee_name, fee_summary)``。"""
    text = str(business_name or "").strip()
    if not text:
        return "", ""

    match = FEE_BRACKET_RE.search(text)
    if match is None:
        return "", text

    fee_name = (match.group(1) or match.group(2) or "").strip()
    fee_summary = FEE_BRACKET_RE.sub("", text).strip()
    return fee_name, fee_summary


def split_multi(value):
    """把单元格里的多值拆成去空白、去空项的列表。"""
    if value is None:
        return []
    return [part.strip() for part in MULTI_VALUE_SPLIT_RE.split(str(value))
            if part.strip()]


def pairs_by_position(left_cell, right_cell):
    """两个多值单元格按相同下标一一配对。"""
    left, right = split_multi(left_cell), split_multi(right_cell)
    return [(l or "", r or "") for l, r in zip_longest(left, right, fillvalue="")]


def dedup(values):
    """按首次出现顺序去重，跳过空值。"""
    seen, out = set(), []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


# 场景交集
def expand_user_scenes(rows):
    """F09633 的多行 → ``[(scene, reason), ...]``，行内多值按位置展开。"""
    pairs = []
    for row in rows:
        for scene, reason in pairs_by_position(row.get("scene"), row.get("reason")):
            if scene:
                pairs.append((scene, reason))
    return pairs


def intersect_scenes(user_pairs, product_scenes):
    """用户场景 ∩ 产品场景，返回 ``(matched_scenes, pain_points)``。"""
    product_set = set(product_scenes)
    matched, pain = [], []
    for scene, reason in user_pairs:
        if scene not in product_set:
            continue
        if scene not in matched:
            matched.append(scene)
        if reason:
            pain.append(reason)
    return matched, pain


def order_scenes_by_priority(scenes, rank_rows, *, allow_fallback=None):
    """把交集场景按 F09627 的 RN 排成候选顺序，返回 ``(ordered, ranked_set)``。"""
    if allow_fallback is None:
        allow_fallback = ALLOW_ENTRY_SCRIPT_FALLBACK

    ranks = {}
    for row in rank_rows:
        scene, rn = row.get("scene"), row.get("rn")
        if not scene or rn is None:
            continue
        try:
            ranks[str(scene)] = int(rn)
        except (TypeError, ValueError):
            continue

    ranked_set = set(ranks)

    def sort_key(item):
        index, scene = item
        if scene in ranks:
            rn = ranks[scene] if SMALLER_RN_HIGHER_PRIORITY else -ranks[scene]
            return (0, rn, index)
        return (1, 0, index)

    ordered = [scene for _, scene in sorted(enumerate(scenes), key=sort_key)]
    if not allow_fallback:
        ordered = [scene for scene in ordered if scene in ranked_set]
    return ordered, ranked_set


def expand_retain_pairs(rows):
    """F09632 多行 → ``[(拒绝原因, 挽留话术), ...]``，单元格多值按位置配对。"""
    pairs = []
    for row in rows:
        for reason, huashu in pairs_by_position(row.get("reject_reason"),
                                                row.get("wl_huashu")):
            if huashu:
                pairs.append((reason, huashu))
    return pairs


def extract_scripts(rows, column):
    """取某话术列并去重，跳过空值。"""
    return dedup([str(row.get(column) or "").strip() for row in rows])


# 话术标签占位符（#标签名& → IOP 的 ITEMVALUE）
def collect_tag_names(record):
    """收集三段话术文本里出现过的 ``#标签名&`` 标签名，保序去重。"""
    record = record or {}
    texts = [str(record.get("entry_script") or "")]
    texts.extend(str(x) for x in (record.get("marketing_scripts") or []))
    for pair in record.get("retain_pairs") or []:
        # 挽留段由「拒绝原因 + 挽留话术」拼成，两部分都允许带标签占位符
        texts.extend(str(part) for part in pair)
    names, seen = [], set()
    for text in texts:
        for match in TAG_PLACEHOLDER_RE.finditer(text):
            name = match.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def replace_tag_placeholders(text, tag_values):
    """把文本里的 ``#标签名&`` 换成 ``tag_values[标签名]``；取不到值原样保留。"""
    if not text or not tag_values:
        return str(text or "")

    def _sub(match):
        name = match.group(1).strip()
        value = tag_values.get(name)
        return str(value) if value is not None else match.group(0)

    return TAG_PLACEHOLDER_RE.sub(_sub, str(text))


def apply_tag_values(record, tag_values):
    """把替换结果写回 record 的三段话术字段，返回浅拷贝（不改入参）。"""
    if not record or not tag_values:
        return record
    out = dict(record)
    out["entry_script"] = replace_tag_placeholders(
        record.get("entry_script"), tag_values)
    out["marketing_scripts"] = [
        replace_tag_placeholders(x, tag_values)
        for x in (record.get("marketing_scripts") or [])]
    out["retain_pairs"] = [
        tuple(replace_tag_placeholders(part, tag_values) for part in pair)
        for pair in (record.get("retain_pairs") or [])]
    return out


# 话术拼装
def pick_retain_pair(retain_pairs, *, rng=None):
    """从「拒绝原因 + 挽留话术」的召回里随机取一条，空召回返回 ``None``。

    拒绝原因与挽留话术已按位置配成一条召回，随机以「条」为单位，
    不会把原因和话术拆开错配。``rng`` 可注入 ``random.Random(seed)``
    便于离线复现；缺省走全局 ``random``，每次拼装独立随机。
    """
    pairs = list(retain_pairs or [])
    if not pairs:
        return None
    chooser = rng if rng is not None else random
    return chooser.choice(pairs)


def build_output_script(record, *, omit=(), rng=None):
    """把 6 个输出字段拼成 HTML 话术。

    挽留段只输出 ``pick_retain_pair`` 从召回里随机选中的一条；痛点诊断 /
    营销话术仍输出全部召回。``rng`` 仅用于测试时固定随机源。
    """
    record = record or {}

    def line(label, value):
        return f"<strong>【{label}】</strong>{value or EMPTY_TEXT}"

    pain_points = record.get("pain_points") or []
    marketing = record.get("marketing_scripts") or []
    retain = record.get("retain_pairs") or []

    pain_text = "<br>".join(str(x) for x in pain_points if x)
    marketing_text = "<br>".join(str(x) for x in marketing if x)
    retain_pair = pick_retain_pair(retain, rng=rng)
    if retain_pair:
        reason, huashu = retain_pair
        retain_text = (
            f'<strong><span style="color:blue;">{reason or EMPTY_TEXT}：</span></strong>'
            f'{huashu or EMPTY_TEXT}'
        )
    else:
        retain_text = ""

    sections = (
        ("资费名称", record.get("fee_name")),
        ("资费摘要", record.get("fee_summary")),
        ("痛点诊断", pain_text),
        ("切入话术", record.get("entry_script")),
        ("营销话术", marketing_text),
        ("挽留话术", retain_text),
    )
    omitted = set(omit)
    return "<br>".join(line(label, value) for label, value in sections
                       if label not in omitted)


__all__ = [
    "split_fee_name", "split_multi", "pairs_by_position", "dedup",
    "expand_user_scenes", "intersect_scenes", "order_scenes_by_priority",
    "expand_retain_pairs", "extract_scripts", "pick_retain_pair",
    "build_output_script",
    "collect_tag_names", "replace_tag_placeholders", "apply_tag_values",
]
