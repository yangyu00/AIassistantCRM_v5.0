# -*- coding: utf-8 -*-
"""控损话术对象：把 ``strategy_name`` 里的自定义标签转成 HTML。"""
from __future__ import annotations

import re

from services.product_info_raw import ProductInfoRaw

_DOLLAR_TAG_RE = re.compile(r"<\$([^>]+)>")
_COLOR_TAG_RE = re.compile(r"<#([0-9A-Fa-f]{6})>")
_CLOSE_TAG_RE = re.compile(r"</>")
_COLOR_CODE_RE = re.compile(r"#([0-9A-Fa-f]{6})")


def parse_custom_tags(text: str) -> str:
    """自定义标签 → HTML，纯函数、可单独单测。

    支持 ``<$B>`` / ``<$I>`` / ``<$U>`` / ``<#RRGGBB>`` 及组合标签
    ``<$B$I#RRGGBB>``；``</>`` 按入栈逆序闭合所有已开启的标签。
    """
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


class ProductInfoByLossControl(ProductInfoRaw):
    """控损业务话术对象：只消费 ``record['strategy_name']``。"""

    def __init__(self, product_id, custom_label_info, selling_points, record=None,
                 portrait_cache=None):
        super().__init__(product_id, custom_label_info, selling_points,
                         portrait_cache)
        self.product_name = "控损业务专用"
        self.record = record
        self.output_script = self._build_output_script()

    def _build_output_script(self):
        raw = (self.record or {}).get("strategy_name", "")
        if not raw:
            return "无历史话术记录"
        return parse_custom_tags(raw).strip().replace("\n", "<br>")

    def __str__(self):
        return (f'Product ID: {self.product_id}\n'
                f'Product Name: {self.product_name}\n'
                f'queried_label_ids: {self.queried_label_ids}\n'
                f'queried_label_nvs: {self.queried_label_nvs}\n'
                f'auxiliary_portrait: {self.auxiliary_portrait}\n'
                f'output_script: {self.output_script}')


__all__ = ["parse_custom_tags", "ProductInfoByLossControl"]
