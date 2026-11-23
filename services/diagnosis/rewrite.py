"""诊断业务对象：把结构化 record 拼成 ``output_script``。"""
from __future__ import annotations

from services.product_info_raw import ProductInfoBase

from .fallback import FALLBACK_OMIT_SECTIONS
from .logic import build_output_script


class ProductInfoByDiagnosis(ProductInfoBase):
    """诊断产品对象：命中 F09629（或卖点维表兜底）后由工厂创建。"""

    def __init__(self, product_id, record=None):
        super().__init__(product_id, product_name="")
        self.record = record or {}
        self.product_name = self.record.get("business_name") or ""
        omit = (FALLBACK_OMIT_SECTIONS if self.record.get("is_fallback")
                else ())
        self.output_script = build_output_script(self.record, omit=omit)

    def _extra_str_lines(self):
        r = self.record
        lines = []
        if r.get("is_fallback"):
            lines.append(f'fallback: {r.get("fallback_reason")}')
        lines += [
            f'business_type: {r.get("business_type")}',
            f'matched_scenes: {r.get("matched_scenes")}',
            f'top_scene: {r.get("top_scene")} ({r.get("top_scene_source")})',
            f'pain_points: {r.get("pain_points")}',
            f'marketing_scripts: {r.get("marketing_scripts")}',
            f'retain_pairs: {r.get("retain_pairs")}',
        ]
        return lines


__all__ = ["ProductInfoByDiagnosis"]
