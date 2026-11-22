# -*- coding: utf-8 -*-
"""控损话术对象：把 ``strategy_name`` 里的自定义标签转成 HTML。"""
from __future__ import annotations

import re

from services.product_info_raw import ProductInfoBase

_DOLLAR_TAG_RE = re.compile(r"<\$([^>]+)>")
_COLOR_TAG_RE = re.compile(r"<#([0-9A-Fa-f]{6})>")
_CLOSE_TAG_RE = re.compile(r"</>")
_COLOR_CODE_RE = re.compile(r"#([0-9A-Fa-f]{6})")


def parse_custom_tags(text: str) -> str:
    """自定义标签 → HTML，纯函数、可单独单测。"""
    result, tag_stack = [], []
    i, length = 0, len(text)

    while i < length:
        dollar = _DOLLAR_TAG_RE.search(text, i)
        color = _COLOR_TAG_RE.search(text, i)
        close = _CLOSE_TAG_RE.search(text, i)

        pos_dollar = dollar.start() if dollar else length
        pos_color = color.start() if color else length
        pos_close = close.start() if close else length
        next_pos = min(pos_dollar, pos_color, pos_close)

        # 后面没有标签了，追加剩余文本并退出
        if next_pos == length:
            result.append(text[i:])
            break

        result.append(text[i:next_pos])

        if next_pos == pos_dollar:                       # <$...> 组合标签
            content = dollar.group(1)
            for flag, html in (("B", "<b>"), ("I", "<i>"), ("U", "<u>")):
                if flag in content:
                    result.append(html)
                    tag_stack.append(flag.lower())
            code = _COLOR_CODE_RE.search(content)
            if code:
                result.append(f'<span style="color: #{code.group(1)}">')
                tag_stack.append("span")
            i = dollar.end()
        elif next_pos == pos_color:                      # <#RRGGBB>
            result.append(f'<span style="color: #{color.group(1)}">')
            tag_stack.append("span")
            i = color.end()
        else:                                            # </>
            while tag_stack:
                tag = tag_stack.pop()
                result.append("</span>" if tag == "span" else f"</{tag}>")
            i = close.end()

    return "".join(result)


class ProductInfoByLossControl(ProductInfoBase):
    """控损业务话术对象：只消费 ``record['strategy_name']``。"""

    def __init__(self, product_id, record=None):
        super().__init__(product_id, product_name="控损业务专用")
        self.record = record
        self.output_script = self._build_output_script()

    def _build_output_script(self):
        raw = (self.record or {}).get("strategy_name", "")
        if not raw:
            # 业务库未就绪 / 查无记录：按空记录出话术，不落到诊断 / 兜底
            return "无历史话术记录"
        return parse_custom_tags(raw).strip().replace("\n", "<br>")


__all__ = ["parse_custom_tags", "ProductInfoByLossControl"]
