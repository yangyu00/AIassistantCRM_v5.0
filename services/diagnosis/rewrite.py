# -*- coding: utf-8 -*-
"""诊断新线业务对象：把结构化 record 拼成 ``output_script``。

继承 ``ProductInfoRaw`` 以保持与商机回捞 / 控损一致的对象形状；话术重组在业务侧
执行，库里始终保存原始文本。

``record.is_fallback`` 为真（卖点维表兜底）时省略「痛点诊断」「营销话术」两段，
其余四段照常拼装；两类 record 都由 ``build_output_script()`` 输出，样式一致。
"""
from __future__ import annotations

from services.product_info_raw import ProductInfoRaw

from .fallback import FALLBACK_OMIT_SECTIONS
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
        omit = (FALLBACK_OMIT_SECTIONS if self.record.get("is_fallback")
                else ())
        self.output_script = build_output_script(self.record, omit=omit)

    def __str__(self):
        r = self.record
        lines = [
            f'Product ID: {self.product_id}',
            f'Product Name: {self.product_name}',
            f'queried_label_ids: {self.queried_label_ids}',
            f'queried_label_nvs: {self.queried_label_nvs}',
            f'auxiliary_portrait: {self.auxiliary_portrait}',
        ]
        if r.get("is_fallback"):
            lines.append(
                f'fallback: {r.get("fallback_reason")} '
                f'hit={r.get("hit_label_id")} @{r.get("hit_tag_index")}')
        lines += [
            f'business_type: {r.get("business_type")}',
            f'matched_scenes: {r.get("matched_scenes")}',
            f'top_scene: {r.get("top_scene")} ({r.get("top_scene_source")})',
            f'pain_points: {r.get("pain_points")}',
            f'marketing_scripts: {r.get("marketing_scripts")}',
            f'retain_pairs: {r.get("retain_pairs")}',
            f'output_script: {self.output_script}',
        ]
        return "\n".join(lines)


__all__ = ["ProductInfoByDiagnosis"]
