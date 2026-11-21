# -*- coding: utf-8 -*-
"""在线话术业务路由：``product_id`` → 数据源 → 产品对象。

``main.py`` 只做 HTTP 编排（入参校验、IOP 标签提供者、画像缓存、响应组装），
「这个产品 ID 查哪张库、用哪个对象拼话术」全部收在本模块，三类业务平级：

| 顺序 | 业务 | 数据源 | 对象 |
|---|---|---|---|
| 1 | 商机回捞（3 个 ID） | ``reader.query(avl, telnum)`` | ``ProductInfoByOpportunityRecall`` |
| 2 | 控损（1 个 ID，有记录才命中） | ``reader.query(avl, telnum)`` | ``ProductInfoByLossControl`` |
| 3 | 诊断新线 | ``build_diagnosis_record()``；任一环节失败改用卖点维表兜底 | ``ProductInfoByDiagnosis`` |
| 4 | 兜底 | — | ``ProductInfoRaw`` |

顺序不可调换。控损分支未查到记录时**不短路**，继续落诊断与兜底（与重构前
``main.py`` 的行为一致）。诊断新线不维护静态 product_id 映射：``F09629``
本身就是「产品ID → 策略 / 产品」映射表。诊断取数失败（db 查空 / IOP 无场景
标签 / 场景交集为空）时由 ``diagnosis/fallback.py`` 用卖点维表兜底，仍返回
``ProductInfoByDiagnosis``（``code: "0"``）；维表里也没有该 product_id 时才
落 ``code: "2"``。

数据访问层 ``services.reader`` 只认 AVL 前缀，完全不知道产品 ID；两张
``product_id → AVL 前缀`` 静态表是**本模块唯一维护点**。卖点维表读取侧
``services.dimensions`` 按 ``product_id`` 取标签 ID 子集，由 ``main.py``
在请求开始时批量查好、经 ``selling_points`` 传入；本模块按业务**只转发给
真正需要的一方**：

- 商机对象：接收 IOP 标签、维表行映射与 ``portrait_cache``，收进
  ``AuxiliaryPortrait`` 组件；
- 诊断取数 / 兜底：接收 IOP 标签与维表行映射，**不进诊断对象**；
- 控损对象：只接收 ``record``，上述三项全部截断（旧版会原样透传）；
- 兜底对象：只接收维表行映射，用于日志里的产品名。

IOP 依赖（``IopNeed`` / ``iop_need()``）也声明在这里，因为它由业务归属派生；
`IopLabelProvider` 与 `portrait_cache` 仍留在 `main.py`，按请求隔离。
"""
from __future__ import annotations

from enum import Enum

from services import reader
from services.diagnosis import ProductInfoByDiagnosis, build_diagnosis_record
from services.loss_control import ProductInfoByLossControl
from services.opportunity_recall import ProductInfoByOpportunityRecall
from services.product_info_raw import ProductInfoRaw

# ===== 业务路由：product_id → AVL 前缀（本模块是唯一维护点）=====
OPPORTUNITY_RECALL_AVLS = {
    "202608041615335173": "F09554",  # 号卡业务
    "202608041615335172": "F09555",  # 套餐业务
    "202608041615335174": "F09556",  # 宽带业务
}
LOSS_CONTROL_AVLS = {
    "202609180955095402": "F09591",  # 控损
}
OPPORTUNITY_RECALL_PRODUCT_IDS = frozenset(OPPORTUNITY_RECALL_AVLS)
LOSS_CONTROL_PRODUCT_IDS = frozenset(LOSS_CONTROL_AVLS)
# =====================================================================


class IopNeed(Enum):
    """各业务对 IOP 标签的依赖。"""
    NONE = "none"              # 完全不取标签
    PORTRAIT = "portrait"      # 取标签并据此计算用户画像
    RAW_LABELS = "raw_labels"  # 只取原始标签，不做画像规则转换


def iop_need(product_id):
    """按产品 ID 声明标签依赖，业务归属由本模块的路由表派生。

    控损话术只由数据库字段拼装，不依赖用户标签，声明为 NONE；商机回捞
    需要标签拼装用户画像；其余产品 ID（诊断新线 / 兜底）只取原始标签、
    不做画像规则转换。诊断正常链路不消费标签，但**卖点维表兜底**要用 IOP
    返回判定「匹配结果2」里哪个场景标签 ID 命中（见 diagnosis/fallback.py），
    所以这里必须取标签。
    """
    if product_id in OPPORTUNITY_RECALL_PRODUCT_IDS:
        return IopNeed.PORTRAIT
    if product_id in LOSS_CONTROL_PRODUCT_IDS:
        return IopNeed.NONE
    return IopNeed.RAW_LABELS


# 前三类对象返回 code "0"，其余（ProductInfoRaw 兜底）返回 code "2"。
# 按类型判定，不靠 product_name 是否为空。
BUSINESS_OBJECT_TYPES = (
    ProductInfoByOpportunityRecall,
    ProductInfoByLossControl,
    ProductInfoByDiagnosis,
)


async def build_product_object(product_id, telnum, *, custom_label_info,
                               selling_points, portrait_cache=None):
    """按四类路由顺序构造一个产品对象；返回对象，由调用方按类型判 code。

    调用方（``main.py``）已按 ``iop_need()`` 准备好 ``custom_label_info`` 与
    ``portrait_cache``：声明 NONE 传 ``{}`` / ``None``，PORTRAIT 传标签与请求级
    画像缓存，RAW_LABELS 传标签与 ``None``。``selling_points`` 是
    ``dimensions.load_selling_points()`` 在请求开始时批量查回的
    ``{product_id: SellingPointRow}``。

    工厂签名保持统一，但**每个分支只传该业务真正消费的参数**：控损 / 诊断 /
    兜底对象不再持有 IOP 标签、画像缓存与画像相关字段。维表未命中或库缺失时
    映射为空 dict，商机画像自然降级为空。
    """
    # 1. 商机回捞（3 个 ID）：即使库里无记录也返回业务对象；只有它拿画像依赖
    if product_id in OPPORTUNITY_RECALL_PRODUCT_IDS:
        avl = OPPORTUNITY_RECALL_AVLS[product_id]
        record = await reader.query(avl, telnum)
        return ProductInfoByOpportunityRecall(
            product_id, custom_label_info, selling_points, record, portrait_cache)

    # 2. 控损：有记录才命中；无记录时继续往下走（不 continue）
    if product_id in LOSS_CONTROL_PRODUCT_IDS:
        avl = LOSS_CONTROL_AVLS[product_id]
        record = await reader.query(avl, telnum)
        if record:
            # 只消费 strategy_name：标签 / 维表行 / 画像缓存都不透传
            return ProductInfoByLossControl(product_id, record)

    # 3. 诊断新线：7 张表关联；任一查询空 / IOP 无场景标签 / 交集为空时
    #    build_diagnosis_record 会改用卖点维表兜底（见 diagnosis/fallback.py），
    #    所以 IOP 标签与维表行在这里一次性消费完毕，对象只拿结构化 record。
    diagnosis_record = await build_diagnosis_record(
        product_id, telnum,
        custom_label_info=custom_label_info,
        selling_points=selling_points)
    if diagnosis_record:
        return ProductInfoByDiagnosis(product_id, diagnosis_record)

    # 4. 兜底：只需 ID 与维表行映射（用于日志里的产品名）
    return ProductInfoRaw(product_id, selling_points)


__all__ = [
    "IopNeed", "iop_need", "build_product_object", "BUSINESS_OBJECT_TYPES",
    "OPPORTUNITY_RECALL_AVLS", "LOSS_CONTROL_AVLS",
    "OPPORTUNITY_RECALL_PRODUCT_IDS", "LOSS_CONTROL_PRODUCT_IDS",
]
