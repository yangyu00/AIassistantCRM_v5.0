# -*- coding: utf-8 -*-
"""商机回捞话术对象：把 F09554/55/56 的单条记录拼成四段话术 + 辅助画像。"""
from __future__ import annotations

from services.product_info_raw import ProductInfoRaw


class ProductInfoByOpportunityRecall(ProductInfoRaw):
    """号卡 / 套餐 / 宽带三类商机回捞的业务对象。

    产品名按 ``product_id`` 取 ``PRODUCT_NAMES``；标签 ID 子集与辅助画像由基类
    按卖点维表行拼装，本类只负责把 record 的四段字段与画像拼成 HTML。
    """

    PRODUCT_NAMES = {
        "202608041615335173": "号卡业务回捞商机",
        "202608041615335172": "套餐业务回捞商机",
        "202608041615335174": "宽带业务回捞商机",
    }

    def __init__(self, product_id, custom_label_info, selling_points, record=None,
                 portrait_cache=None):
        super().__init__(product_id, custom_label_info, selling_points,
                         portrait_cache)
        self.record = record
        self.product_name = self.PRODUCT_NAMES.get(product_id)
        self.output_script = self._build_output_script()

    def _build_output_script(self):
        """四段固定；空值填「无」；辅助画像非空时追加在最后。"""
        lines = []
        rec = self.record or {}
        if rec:
            lines.extend([
                f'<strong>【通话总结】</strong>{rec.get("content_abstract") or "无"}',
                '<strong>【用户需求】</strong>'
                f'<strong><span style="color:blue;">{rec.get("type_xl")}：</span></strong>'
                f'{rec.get("cust_demand") or "无"}',
                f'<strong>【已推荐政策】</strong>{rec.get("rec_strategy") or "无"}',
                '<strong>【拒绝原因】</strong>',
                '<strong><span style="color:blue;">拒绝原因：</span></strong>'
                f'{rec.get("reject_reason") or "无"}',
                '<strong><span style="color:blue;">具体表述：</span></strong>'
                f'{rec.get("reason_note") or "无"}',
            ])
        if self.auxiliary_portrait:
            lines.append(self.auxiliary_portrait)
        return "\n".join(lines).strip().replace("\n", "<br>")

    def __str__(self):
        return (f'Product ID: {self.product_id}\n'
                f'Product Name: {self.product_name}\n'
                f'queried_label_ids: {self.queried_label_ids}\n'
                f'queried_label_nvs: {self.queried_label_nvs}\n'
                f'auxiliary_portrait: {self.auxiliary_portrait}\n'
                f'output_script: {self.output_script}')


__all__ = ["ProductInfoByOpportunityRecall"]
