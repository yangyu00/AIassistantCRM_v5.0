# -*- coding: utf-8 -*-
"""``services``：在线话术业务层（读取 + 路由 + 三类业务的话术重组）。

分层（详见包内 ``README.md``）::

    services/
      product_info_raw.py        极简公共基类 ProductInfoBase + 商机专属画像组件
                                 AuxiliaryPortrait + 兜底对象 ProductInfoRaw
      reader.py                  业务库在线读取侧（AVL 前缀，单条 / 多行 / IN）
      dimensions.py              卖点维表在线读取侧（标签 ID / 产品名 / 兜底话术 / 标签N 原文）
      routing.py                 业务路由表、IOP 查询依赖声明、产品对象工厂
      opportunity_recall/        商机回捞（ProductInfoByOpportunityRecall）
      loss_control/              控损（ProductInfoByLossControl）
      diagnosis/                 诊断新线（取数 / 纯逻辑 / 六段话术 / 卖点维表兜底）

    三类业务包一律以**业务名**命名，包内 ``rewrite.py`` 承载业务对象，
    ``__init__.py`` 显式导出；不保留按 spec 编号的历史目录名与旧类名别名。

对象模型：所有对象只继承 ``ProductInfoBase``（ID / 名称 / 话术 + 统一日志头）；
辅助画像用组合挂在商机对象的 ``AuxiliaryPortrait`` 上，由组件自己拼装并缓存
在实例上；IOP 标签与卖点维表行映射不再让控损 / 诊断 / 兜底被动继承。
``build_product_object()`` 按业务只传各自需要的参数。

``main.py`` 只做 HTTP 编排，业务对象一律通过 ``build_product_object()`` 取得。

**包根零 I/O**：所有业务对象与读取侧都在 ``__getattr__`` 里延迟导出，
``import services`` 不读 ``.env``、不连库、不加载 fastapi / aiohttp。
显式访问 ``services.reader`` 或任一业务对象时才会装配本层 ``db_config``
的读取配置；各 db 文件、表字段与查询声明都在本层独立维护。
"""
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
    # 三类业务对象（业务包按业务名命名，对外用语义化类名）
    "ProductInfoByOpportunityRecall": (
        "opportunity_recall", "ProductInfoByOpportunityRecall"),
    "ProductInfoByLossControl": (
        "loss_control", "ProductInfoByLossControl"),
    "ProductInfoByDiagnosis": ("diagnosis", "ProductInfoByDiagnosis"),
    "build_diagnosis_record": ("diagnosis", "build_diagnosis_record"),
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
               "opportunity_recall", "loss_control", "diagnosis")


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
    # 公共基类、画像组件与兜底对象
    "ProductInfoBase", "AuxiliaryPortrait", "ProductInfoRaw",
    "build_auxiliary_portrait",
    # 卖点维表读取侧
    "SellingPointRow", "split_label_ids", "load_selling_points",
    # 三类业务对象
    "ProductInfoByOpportunityRecall", "ProductInfoByLossControl",
    "ProductInfoByDiagnosis", "build_diagnosis_record",
    # 路由与 IOP 查询依赖
    "need_labels", "build_product_object", "BUSINESS_OBJECT_TYPES",
    "OPPORTUNITY_RECALL_AVLS", "LOSS_CONTROL_AVLS",
    "OPPORTUNITY_RECALL_PRODUCT_IDS", "LOSS_CONTROL_PRODUCT_IDS",
]
