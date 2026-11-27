"""``services``：在线话术业务层（读取 + 路由 + 四类业务的话术重组）。"""
from __future__ import annotations

# 延迟导出：(模块名, 属性名)。模块名可含点号（子包内的模块）。
_LAZY_EXPORTS = {
    # 公共基类、商机画像组件与兜底对象
    "ProductInfoBase": ("product_info_raw", "ProductInfoBase"),
    "AuxiliaryPortrait": ("product_info_raw", "AuxiliaryPortrait"),
    "ProductInfoRaw": ("product_info_raw", "ProductInfoRaw"),
    "build_auxiliary_portrait": ("product_info_raw", "build_auxiliary_portrait"),
    # 卖点维表读取侧（显式访问才装配本层 db_config 配置）
    "SellingPointRow": ("dimensions", "SellingPointRow"),
    "split_label_ids": ("dimensions", "split_label_ids"),
    "load_selling_points": ("dimensions", "load_selling_points"),
    "load_label_name_mapping": ("dimensions", "load_label_name_mapping"),
    # 四类业务对象（业务包按业务名命名，对外用语义化类名）
    "ProductInfoByOpportunityRecall": (
        "opportunity_recall", "ProductInfoByOpportunityRecall"),
    "ProductInfoByLossControl": (
        "loss_control", "ProductInfoByLossControl"),
    "ProductInfoByDiagnosis": ("diagnosis", "ProductInfoByDiagnosis"),
    "build_diagnosis_record": ("diagnosis", "build_diagnosis_record"),
    "ProductInfoBySellingPoint": (
        "selling_point_script", "ProductInfoBySellingPoint"),
    "build_selling_point_object": (
        "selling_point_script", "build_selling_point_object"),
    # 路由与 IOP 查询依赖
    "need_labels": ("routing", "need_labels"),
    "build_product_object": ("routing", "build_product_object"),
    "BUSINESS_OBJECT_TYPES": ("routing", "BUSINESS_OBJECT_TYPES"),
    "OPPORTUNITY_RECALL_AVLS": ("routing", "OPPORTUNITY_RECALL_AVLS"),
    "LOSS_CONTROL_AVLS": ("routing", "LOSS_CONTROL_AVLS"),
    "OPPORTUNITY_RECALL_PRODUCT_IDS": (
        "routing", "OPPORTUNITY_RECALL_PRODUCT_IDS"),
    "LOSS_CONTROL_PRODUCT_IDS": ("routing", "LOSS_CONTROL_PRODUCT_IDS"),
}


_SUBMODULES = ("reader", "dimensions", "routing",
               "opportunity_recall", "loss_control", "diagnosis",
               "selling_point_script")


def __getattr__(name):
    if name in _SUBMODULES:
        # 子模块：返回模块本身，供 `from services import reader` 使用。
        import importlib
        return importlib.import_module("." + name, __name__)
    if name in _LAZY_EXPORTS:
        import importlib
        mod, attr = _LAZY_EXPORTS[name]
        return getattr(importlib.import_module("." + mod, __name__), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS) | set(_SUBMODULES))


__all__ = [
    # 子模块
    "reader", "dimensions", "routing",
    "opportunity_recall", "loss_control", "diagnosis",
    "selling_point_script",
    # 公共基类、画像组件与兜底对象
    "ProductInfoBase", "AuxiliaryPortrait", "ProductInfoRaw",
    "build_auxiliary_portrait",
    # 卖点维表读取侧
    "SellingPointRow", "split_label_ids", "load_selling_points",
    "load_label_name_mapping",
    # 四类业务对象
    "ProductInfoByOpportunityRecall", "ProductInfoByLossControl",
    "ProductInfoByDiagnosis", "build_diagnosis_record",
    "ProductInfoBySellingPoint", "build_selling_point_object",
    "need_labels", "build_product_object", "BUSINESS_OBJECT_TYPES",
    "OPPORTUNITY_RECALL_AVLS", "LOSS_CONTROL_AVLS",
    "OPPORTUNITY_RECALL_PRODUCT_IDS", "LOSS_CONTROL_PRODUCT_IDS",
]
