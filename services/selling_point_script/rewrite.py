"""卖点维表话术业务对象：与商机回捞 / 控损 / 诊断平级的第四类业务。

构造只收 ``product_id`` 与结构化 record，不收 ``custom_label_info``、
不收诊断 record、不查任何 AVL 表；话术原文里也不存在要替换的字段。
"""
from __future__ import annotations

from services.product_info_raw import ProductInfoBase

from .logic import build_output_script, extract_selling_point_fields


class ProductInfoBySellingPoint(ProductInfoBase):
    """由卖点维表一行拼出的话术对象（``code: "0"``）。"""

    def __init__(self, product_id, record=None):
        super().__init__(product_id, product_name="")
        self.record = record or {}
        self.product_name = self.record.get("business_name") or ""
        self.output_script = build_output_script(self.record)

    def _extra_str_lines(self):
        return [
            f'entry_script: {self.record.get("entry_script")}',
            f'retain_pairs: {self.record.get("retain_pairs")}',
        ]


def build_selling_point_object(product_id, selling_points=None):
    """维表行 → 业务对象；无该行或「产品名称」为空返回 ``None``。

    ``None`` 只表示本业务不适用，由 ``routing`` 继续降级到 ``ProductInfoRaw``，
    不在这里做任何兜底话术拼装。
    """
    row = (selling_points or {}).get(product_id)
    record = extract_selling_point_fields(product_id, row)
    if record is None:
        return None
    return ProductInfoBySellingPoint(product_id, record)


__all__ = ["ProductInfoBySellingPoint", "build_selling_point_object"]
