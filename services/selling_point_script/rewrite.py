"""卖点维表话术业务对象：复现上一版本 ``ProductInfoRewrite`` 的输出。

构造收 ``product_id`` / 维表行映射 / 请求级 IOP 返回 / 标签显示名映射，
不收诊断 record、不查任何 AVL 表；话术原文里也不存在要替换的字段。
"""
from __future__ import annotations

from services.product_info_raw import ProductInfoBase

from .logic import build_output_script, extract_selling_point_fields


class ProductInfoBySellingPoint(ProductInfoBase):
    """由卖点维表一行 + IOP 返回拼出的话术对象（``code: "0"``）。"""

    def __init__(self, product_id, record=None):
        super().__init__(product_id, product_name="")
        self.record = record or {}
        self.product_name = self.record.get("business_name") or ""
        self.output_script = build_output_script(self.record)

    def _extra_str_lines(self):
        r = self.record
        return [
            f'selling_point: {r.get("selling_point_block")}',
            f'auxiliary_portrait: {r.get("auxiliary_portrait")}',
            f'cut_in_statement: {r.get("cut_in_statement")}',
            f'retain_statement: {r.get("retain_statement")}',
        ]


def build_selling_point_object(product_id, selling_points,
                               custom_label_info=None,
                               label_name_mapping=None):
    """db 行 + IOP 返回 → 业务对象；维表无该行 / 产品名称为空返回 ``None``。

    ``None`` 只表示本业务不适用，由 ``routing`` 继续降级到 ``ProductInfoRaw``。
    """
    row = (selling_points or {}).get(product_id)
    record = extract_selling_point_fields(
        product_id, row, custom_label_info, label_name_mapping)
    if record is None:
        return None
    return ProductInfoBySellingPoint(product_id, record)


__all__ = ["ProductInfoBySellingPoint", "build_selling_point_object"]
