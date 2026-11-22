# -*- coding: utf-8 -*-
"""诊断的**卖点维表兜底**：db 查空 / 场景交集为空 / 六项输出全空时启用。"""
from __future__ import annotations

import re

from .logic import split_fee_name

# 兜底输出里不出现的段落（痛点诊断、营销话术没有维表来源）
FALLBACK_OMIT_SECTIONS = ("痛点诊断", "营销话术")

# 触发原因（写进 record，便于日志排查）
REASON_DB_EMPTY = "db_empty"
REASON_EMPTY_INTERSECTION = "empty_intersection"
REASON_EMPTY_OUTPUT = "empty_output"

_ENTRY_TITLE = "切入话术"
_RETAIN_TITLE = "拒绝挽留话术"
_SEPARATOR_RE = re.compile(r"[：:]")
# 少量单元格里混入了异常堆栈，不收进话术
_NOISE_PREFIXES = ("Traceback", 'File "')


def _clean_line(line) -> str:
    """去掉 markdown 加粗标记与首尾空白，保留原话术文本。"""
    return str(line).strip().replace("**", "").strip()


def _is_title(line: str, title: str) -> bool:
    return line.rstrip("：: ").strip() == title


def parse_tag_script(text):
    """解析一个话术单元格 → ``(切入话术, [(拒绝原因, 挽留话术), ...])``。"""
    if not text:
        return "", []

    entry_lines, retain_pairs = [], []
    section = None
    for raw in str(text).splitlines():
        line = _clean_line(raw)
        if not line or line.startswith(_NOISE_PREFIXES):
            continue
        if _is_title(line, _ENTRY_TITLE):
            section = "entry"
            continue
        if _is_title(line, _RETAIN_TITLE):
            section = "retain"
            continue
        if section == "entry":
            entry_lines.append(line)
        elif section == "retain":
            match = _SEPARATOR_RE.search(line)
            if match:
                reason = line[:match.start()].strip()
                huashu = line[match.end():].strip()
                if reason or huashu:
                    retain_pairs.append((reason, huashu))
            else:
                retain_pairs.append(("", line))
    return "<br>".join(entry_lines), retain_pairs


def pick_script_text(row) -> str:
    """兜底话术原文：优先「兜底话术」列，其次「标签N」列第一个非空原文。"""
    text = str(row.fallback_script or "").strip()
    if text:
        return text
    for tag_text in row.tag_texts:
        if str(tag_text or "").strip():
            return tag_text
    return ""


def build_fallback_record(product_id, telnum, *, selling_points=None, reason=""):
    """按卖点维表组装兜底 record。"""
    row = (selling_points or {}).get(product_id)
    if row is None or not row.product_name:
        return None

    fee_name, fee_summary = split_fee_name(row.product_name)
    entry_script, retain_pairs = parse_tag_script(pick_script_text(row))
    return {
        "is_fallback": True,
        "fallback_reason": reason,
        "product_id": product_id,
        "telnum": telnum,
        "business_name": row.product_name,
        "fee_name": fee_name,
        "fee_summary": fee_summary,
        "entry_script": entry_script,
        "retain_pairs": retain_pairs,
        # 痛点诊断 / 营销话术无来源：保持空，且输出时整段省略
        "pain_points": [],
        "marketing_scripts": [],
    }


__all__ = [
    "FALLBACK_OMIT_SECTIONS", "REASON_DB_EMPTY",
    "REASON_EMPTY_INTERSECTION", "REASON_EMPTY_OUTPUT",
    "parse_tag_script", "pick_script_text", "build_fallback_record",
]
