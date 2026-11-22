# -*- coding: utf-8 -*-
"""在线话术业务路由：``product_id`` → 数据源 → 产品对象。"""
from __future__ import annotations

from services import reader
from services.diagnosis import ProductInfoByDiagnosis, build_diagnosis_record
from services.loss_control import ProductInfoByLossControl
from services.opportunity_recall import ProductInfoByOpportunityRecall
from services.product_info_raw import ProductInfoRaw

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


def need_labels(product_id):
    """该 ``product_id`` 是否需要查 IOP 标签。"""
    return product_id in OPPORTUNITY_RECALL_PRODUCT_IDS


# 前三类对象返回 code "0"，其余（ProductInfoRaw 兜底）返回 code "2"。
# 按类型判定，不靠 product_name 是否为空。
BUSINESS_OBJECT_TYPES = (
    ProductInfoByOpportunityRecall,
    ProductInfoByLossControl,
    ProductInfoByDiagnosis,
)


async def build_product_object(product_id, telnum, *, custom_label_info,
                               selling_points):
    """按四类路由顺序构造一个产品对象。"""
    # 1. 商机回捞（3 个 ID）：即使库里无记录也返回业务对象，按空记录出四段话术；
    #    只有它拿画像组件
    # 1. 商机回捞：单条记录，唯一持有辅助画像组件
    if product_id in OPPORTUNITY_RECALL_PRODUCT_IDS:
        avl = OPPORTUNITY_RECALL_AVLS[product_id]
        record = await reader.query(avl, telnum)
        return ProductInfoByOpportunityRecall(
            product_id, custom_label_info, selling_points, record)

    # 2. 控损：命中即返回，无记录按空记录出话术
    if product_id in LOSS_CONTROL_PRODUCT_IDS:
        avl = LOSS_CONTROL_AVLS[product_id]
        record = await reader.query(avl, telnum)
        return ProductInfoByLossControl(product_id, record)

    # 3. 诊断：7 张表关联；任一查询空 / 交集为空 / 六项全空时
    #    build_diagnosis_record 会改用卖点维表兜底（见 diagnosis/fallback.py），
    #    维表行在这里一次性消费完毕，对象只拿结构化 record。
    # 3. 诊断：7 表关联，任一环节为空由 build_diagnosis_record 走维表兜底
    diagnosis_record = await build_diagnosis_record(
        product_id, telnum, selling_points=selling_points)
    if diagnosis_record:
        return ProductInfoByDiagnosis(product_id, diagnosis_record)

    # 4. 兜底：只需 ID 与维表行映射（日志产品名）
    return ProductInfoRaw(product_id, selling_points)


__all__ = [
    "need_labels", "build_product_object", "BUSINESS_OBJECT_TYPES",
    "OPPORTUNITY_RECALL_AVLS", "LOSS_CONTROL_AVLS",
    "OPPORTUNITY_RECALL_PRODUCT_IDS", "LOSS_CONTROL_PRODUCT_IDS",
]
