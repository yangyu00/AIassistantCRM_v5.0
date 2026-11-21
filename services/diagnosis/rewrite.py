# -*- coding: utf-8 -*-
"""诊断新线业务对象：把结构化 record 拼成 ``output_script``。

继承 ``ProductInfoRaw`` 以保持与商机回捞 / 控损一致的对象形状；话术重组在业务侧
执行，库里始终保存原始文本。
"""
from __future__ import annotations

from services.product_info_raw import ProductInfoRaw

from .logic import build_output_script


class ProductInfoByDiagnosis(ProductInfoRaw):
    """诊断新线产品对象：命中 F09629 后由 ``main.py`` 创建。

    ``product_name`` 取资费名称（【】内），无括号时退到资费摘要、再到原始
    BUSINESS_NAME，保证响应里的 productName 不为空。
    """

    def __init__(self, product_id, custom_label_info, selling_points,
                 record=None, portrait_cache=None):
        super().__init__(product_id, custom_label_info, selling_points,
                         portrait_cache)
        self.record = record or {}
        self.product_name = (
            self.record.get("fee_name")
            or self.record.get("fee_summary")
            or self.record.get("business_name")
            or ""
        )
        self.output_script = build_output_script(self.record)

    def __str__(self):
        r = self.record
        return (f'Product ID: {self.product_id}\n'
                f'Product Name: {self.product_name}\n'
                f'queried_label_ids: {self.queried_label_ids}\n'
                f'queried_label_nvs: {self.queried_label_nvs}\n'
                f'auxiliary_portrait: {self.auxiliary_portrait}\n'
                f'business_type: {r.get("business_type")}\n'
                f'matched_scenes: {r.get("matched_scenes")}\n'
                f'top_scene: {r.get("top_scene")} ({r.get("top_scene_source")})\n'
                f'pain_points: {r.get("pain_points")}\n'
                f'marketing_scripts: {r.get("marketing_scripts")}\n'
                f'retain_pairs: {r.get("retain_pairs")}\n'
                f'output_script: {self.output_script}')


__all__ = ["ProductInfoByDiagnosis"]
