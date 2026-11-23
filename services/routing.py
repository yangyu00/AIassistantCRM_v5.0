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
    """该 ``product_id`` 是否需要查 IOP 标签。

    商机回捞用标签拼【辅助画像】；诊断链路用标签把三类话术里的
    ``#标签名&`` 换成 IOP 的 ITEMVALUE。诊断由 F09629 动态命中、没有静态
    ID 白名单，因此除控损外一律返回 True；标签由 main.py 的请求级提供者
    保证整请求最多查 1 次 IOP，不会跨请求共享。
    """
    return product_id not in LOSS_CONTROL_PRODUCT_IDS


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
    # 1. 商机回捞（3 个 ID）：单条记录，即使库里无记录也按空记录出四段话术；
    #    唯一持有辅助画像组件
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

    # 3. 诊断：7 表关联 + F09635/IOP 标签值替换，任一环节为空由
    #    build_diagnosis_record 走维表兜底；对象只拿结构化 record，
    #    维表行与请求级标签在这里消费完毕
    diagnosis_record = await build_diagnosis_record(
        product_id, telnum, selling_points=selling_points,
        custom_label_info=custom_label_info)
    if diagnosis_record:
        return ProductInfoByDiagnosis(product_id, diagnosis_record)

    # 4. 兜底：只需 ID 与维表行映射（日志产品名）
    return ProductInfoRaw(product_id, selling_points)


__all__ = [
    "need_labels", "build_product_object", "BUSINESS_OBJECT_TYPES",
    "OPPORTUNITY_RECALL_AVLS", "LOSS_CONTROL_AVLS",
    "OPPORTUNITY_RECALL_PRODUCT_IDS", "LOSS_CONTROL_PRODUCT_IDS",
]
