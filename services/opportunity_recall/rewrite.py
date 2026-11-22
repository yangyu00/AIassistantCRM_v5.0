# -*- coding: utf-8 -*-
"""商机回捞话术对象：把 F09554/55/56 的单条记录拼成四段话术 + 辅助画像。"""
from __future__ import annotations

from services.product_info_raw import AuxiliaryPortrait, ProductInfoBase


class ProductInfoByOpportunityRecall(ProductInfoBase):
    """号卡 / 套餐 / 宽带三类商机回捞的业务对象。"""

    PRODUCT_NAMES = {
        "202608041615335173": "号卡业务回捞商机",
        "202608041615335172": "套餐业务回捞商机",
        "202608041615335174": "宽带业务回捞商机",
    }

    def __init__(self, product_id, custom_label_info, selling_point_rows,
                 record=None):
        super().__init__(product_id,
                         product_name=self.PRODUCT_NAMES.get(product_id))
        self.portrait = AuxiliaryPortrait(
            product_id, custom_label_info, selling_point_rows)
        self.record = record
        self.output_script = self._build_output_script()

    @property
    def selling_point_rows(self):
        """``{product_id: SellingPointRow}``。"""
        return self.portrait.selling_point_rows

    @property
    def queried_label_ids(self):
        return self.portrait.queried_label_ids

    @property
    def queried_label_nvs(self):
        return self.portrait.queried_label_nvs

    @property
    def auxiliary_portrait(self):
        return self.portrait.auxiliary_portrait

    def _build_output_script(self):
        """四段固定。"""
        lines = []
        rec = self.record or {}
        lines.extend([
            f'<strong>【通话总结】</strong>{rec.get("content_abstract") or "无"}',
            '<strong>【用户需求】</strong>'
            f'<strong><span style="color:blue;">{rec.get("type_xl") or "无"}：</span></strong>'
            f'{rec.get("cust_demand") or "无"}',
            f'<strong>【已推荐政策】</strong>{rec.get("rec_strategy") or "无"}',
            '<strong>【拒绝原因】</strong>',
            '<strong><span style="color:blue;">拒绝原因：</span></strong>'
            f'{rec.get("reject_reason") or "无"}',
            '<strong><span style="color:blue;">具体表述：</span></strong>'
            f'{rec.get("reason_note") or "无"}',
        ])
        portrait = self.auxiliary_portrait
        if portrait:
            lines.append(portrait)
        return "\n".join(lines).strip().replace("\n", "<br>")

    def _extra_str_lines(self):
        return [
            f'queried_label_ids: {self.queried_label_ids}',
            f'queried_label_nvs: {self.queried_label_nvs}',
            f'auxiliary_portrait: {self.auxiliary_portrait}',
        ]


__all__ = ["ProductInfoByOpportunityRecall"]
