"""卖点维表话术业务包：只消费 ``selling_points`` 一行，与诊断 8 表零耦合。"""
from __future__ import annotations

from .logic import (
    SELLING_POINT_SECTIONS,
    build_output_script,
    extract_selling_point_fields,
    parse_tag_script,
    pick_script_text,
    split_product_name,
)
from .rewrite import ProductInfoBySellingPoint, build_selling_point_object

__all__ = [
    "ProductInfoBySellingPoint", "build_selling_point_object",
    "SELLING_POINT_SECTIONS", "split_product_name", "parse_tag_script",
    "pick_script_text", "extract_selling_point_fields", "build_output_script",
]
