# -*- coding: utf-8 -*-
"""诊断新线的**卖点维表兜底**：db 查空 / 场景交集为空 / 六项输出全空时启用。

正常链路（``query.py`` 的 7 张表关联）任一环节拿不到结果，就改用卖点维表
``selling_points`` 的这一行，按用户口径组装四段话术：

1. 按 ``product_id`` 取 ``product_name``（产品名称），**与诊断一样**用
   ``split_fee_name()`` 按【】拆成资费名称 / 资费摘要填入；
2. 取 ``fallback_script``（「兜底话术」）原文，为空时回退到「标签N」列里
   第一个非空原文；库里只存原始文本，拆解加工在本层完成；
3. ``parse_tag_script()`` 解析出「切入话术」「拒绝原因 / 挽留话术」填入；
4. 痛点诊断与营销话术没有来源，**这两段不出现在最终输出**（由
   ``FALLBACK_OMIT_SECTIONS`` 控制，不是填「无」）。

若维表里没有该 ``product_id`` 或产品名称为空 → 返回 ``None``，上层维持原有
``code: "2"`` 降级；话术单元格为空时仍输出资费名称 / 摘要，切入与挽留段
为空值占位。本模块纯函数、零 I/O、零第三方依赖，不依赖 IOP 标签。
"""
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
    """解析一个话术单元格 → ``(切入话术, [(拒绝原因, 挽留话术), ...])``。

    单元格结构（样例）：

        切入话术
        基于客户标签特征切入（XXX）：……
        基于问题引导的通用切入：……
        拒绝挽留话术
        价格太高：……
        担心合约期：……

    - 标题行支持 ``切入话术`` / ``切入话术：`` / ``**切入话术**`` 变体；
    - 「切入话术」段收集全部非空行，用 ``<br>`` 拼接（输出是 HTML）；
    - 「拒绝挽留话术」段每行按**首个**全角/半角冒号拆成「拒绝原因：挽留话术」；
      没有冒号的裸话术行也保留，拒绝原因留空；
    - 空单元格 → ``("", [])``。
    """
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
    """兜底话术原文：优先「兜底话术」列，其次「标签N」列第一个非空原文。

    库里只存原始文本，这里只做选择、不做解析；解析统一由
    ``parse_tag_script()`` 完成。
    """
    text = str(row.fallback_script or "").strip()
    if text:
        return text
    for tag_text in row.tag_texts:
        if str(tag_text or "").strip():
            return tag_text
    return ""


def build_fallback_record(product_id, telnum, *, selling_points=None, reason=""):
    """按卖点维表组装兜底 record；维表无该行 / 无产品名称时返回 ``None``。

    返回的 dict 带 ``is_fallback=True`` 与 ``fallback_reason``，
    ``ProductInfoByDiagnosis`` 据此省略痛点诊断 / 营销话术两段。
    """
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
