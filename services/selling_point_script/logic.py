"""卖点维表话术：字段抽取与话术组合，纯函数、零 I/O、零第三方依赖。

本模块与 ``services/diagnosis/`` 完全平级且互不 import：

* 数据来源只有 ``services.dimensions.SellingPointRow``（卖点维表一行），
  不读诊断的 8 张表，也不查 F09635 / IOP 标签；
* 切出的话术原文里不存在 ``#标签名&`` 一类待替换字段，因此这里不做任何
  占位符替换，原文怎么用就怎么拼；
* 以后要改变「从维表里抽取哪些信息」只改 ``extract_selling_point_fields()``，
  要改变「抽出来的信息怎么组合」只改 ``build_output_script()``，两者互不影响。
"""
from __future__ import annotations

import re

# 维表「产品名称」按【】/[] 拆成资费名称 / 资费摘要。
# 刻意不复用 diagnosis.logic.split_fee_name：两个业务各自演进，避免为了一个正则
# 把卖点话术模块重新绑回诊断包。
_BRACKET_RE = re.compile(r"【([^】]*)】|\[([^\[\]]*)\]")
_SEPARATOR_RE = re.compile(r"[：:]")
_NOISE_PREFIXES = ("Traceback", 'File "')

_ENTRY_TITLE = "切入话术"
_RETAIN_TITLE = "拒绝挽留话术"
EMPTY_TEXT = "无"

# 本业务固定输出的四段（痛点诊断 / 营销话术没有维表来源，整段不出现）
SELLING_POINT_SECTIONS = ("资费名称", "资费摘要", "切入话术", "挽留话术")


def split_product_name(product_name):
    """维表「产品名称」→ ``(资费名称, 资费摘要)``；无括号时名称空、摘要全文。"""
    text = str(product_name or "").strip()
    if not text:
        return "", ""
    match = _BRACKET_RE.search(text)
    if match is None:
        return "", text
    name = (match.group(1) or match.group(2) or "").strip()
    summary = _BRACKET_RE.sub("", text).strip()
    return name, summary


def _clean_line(line) -> str:
    """去掉 markdown 加粗标记与首尾空白，保留原话术文本。"""
    return str(line).strip().replace("**", "").strip()


def _is_title(line: str, title: str) -> bool:
    return line.rstrip("：: ").strip() == title


def parse_tag_script(text):
    """一个话术单元格 → ``(切入话术, [(拒绝原因, 挽留话术), ...])``。

    按 ``切入话术`` / ``拒绝挽留话术`` 两个标题分段：前者收集非空行用 ``<br>``
    拼接；后者每行按首个全角 / 半角冒号拆成二元组。原文里的任何占位符都原样
    保留——本业务不做字段替换。
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
    """话术原文：优先「兜底话术」列，其次「标签N」列第一个非空原文。"""
    text = str(getattr(row, "fallback_script", "") or "").strip()
    if text:
        return text
    for tag_text in getattr(row, "tag_texts", ()) or ():
        if str(tag_text or "").strip():
            return tag_text
    return ""


def extract_selling_point_fields(product_id, row):
    """**单一抽取点**：维表一行 → 话术字段 dict；维表无该行 / 产品名称为空返回 ``None``。

    将来若要从维表里多取几列、换一列当主话术、或改标题分段规则，只需改这里
    （以及它调用的小函数），下游组合与业务对象都不用动。
    """
    if row is None:
        return None
    product_name = str(getattr(row, "product_name", "") or "").strip()
    if not product_name:
        return None

    fee_name, fee_summary = split_product_name(product_name)
    entry_script, retain_pairs = parse_tag_script(pick_script_text(row))
    return {
        "product_id": product_id,
        "business_name": product_name,
        "fee_name": fee_name,
        "fee_summary": fee_summary,
        "entry_script": entry_script,
        "retain_pairs": retain_pairs,
    }


def build_output_script(record):
    """**单一组合点**：话术字段 dict → 四段 HTML；空值填「无」。

    与诊断六段模板刻意各自实现：卖点话术以后可能增删段落、改顺序、换配色，
    不应受诊断模板的 ``omit`` 机制约束。
    """
    record = record or {}

    def line(label, value):
        return f"<strong>【{label}】</strong>{value or EMPTY_TEXT}"

    retain = record.get("retain_pairs") or []
    if retain:
        retain_text = "<br>".join(
            f'<strong><span style="color:blue;">{reason or EMPTY_TEXT}：</span></strong>'
            f'{huashu or EMPTY_TEXT}'
            for reason, huashu in retain
        )
    else:
        retain_text = ""

    sections = (
        ("资费名称", record.get("fee_name")),
        ("资费摘要", record.get("fee_summary")),
        ("切入话术", record.get("entry_script")),
        ("挽留话术", retain_text),
    )
    return "<br>".join(line(label, value) for label, value in sections)


__all__ = [
    "SELLING_POINT_SECTIONS", "EMPTY_TEXT",
    "split_product_name", "parse_tag_script", "pick_script_text",
    "extract_selling_point_fields", "build_output_script",
]
