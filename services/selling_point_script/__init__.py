"""卖点维表话术业务包：db 一行 + IOP 返回 → 复现上一版本话术输出。"""
from __future__ import annotations

from .logic import (
    SELLING_POINT_SECTIONS,
    STRONG_TITLES,
    build_auxiliary_portrait,
    build_output_script,
    build_script_dict,
    build_selling_point,
    build_selling_point_section,
    cut_in_str2tuple,
    extract_selling_point_fields,
    parse_fusion_scripts,
    pick_script_text,
    select_script,
    strong_sections,
)
from .rewrite import ProductInfoBySellingPoint, build_selling_point_object

__all__ = [
    "ProductInfoBySellingPoint", "build_selling_point_object",
    "SELLING_POINT_SECTIONS", "STRONG_TITLES", "strong_sections",
    "parse_fusion_scripts", "cut_in_str2tuple", "pick_script_text",
    "build_script_dict", "select_script",
    "build_selling_point_section", "build_selling_point",
    "build_auxiliary_portrait", "extract_selling_point_fields",
    "build_output_script",
]
