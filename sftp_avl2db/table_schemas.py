"""表结构定义：按名字注册，供多个 AVL 复用。"""
from __future__ import annotations

from typing import Final, Mapping

TABLE_SCHEMAS: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    "opportunity_recall": (
        ("op_time", "TEXT"),
        ("call_time", "TEXT"),
        ("product_no", "TEXT NOT NULL"),
        ("type_dl", "VARCHAR(50)"),
        ("type_xl", "VARCHAR(50)"),
        ("content_abstract", "TEXT"),
        ("cust_demand", "TEXT"),
        ("rec_strategy", "TEXT"),
        ("reject_reason", "TEXT"),
        ("reason_note", "TEXT"),
        ("refer_strategy", "TEXT"),
        ("city_id", "INTEGER"),
    ),
    "loss_control": (
        ("op_time", "TEXT"),
        ("user_id", "TEXT"),
        ("product_no", "TEXT NOT NULL"),
        ("city_id", "TEXT"),
        ("strategy_name", "TEXT"),
    ),

    # 列顺序 = AVL 制表符分隔顺序；类型按 xls 的长度/类型（date → TEXT，
    # 便于 delete_expired_sql 按 'YYYY-MM-DD' 字符串比较；bigint → INTEGER）。
    # F09627 / DIM_MARKET_SCENE_RANK_ONCE / 场景优先级
    "scene_rank": (
        ("op_time", "TEXT"),                 # OP_TIME 数据日期
        ("scene", "VARCHAR(255)"),          # SCENE 场景
        ("rn", "INTEGER"),                  # RN 场景优先级排序
    ),
    # F09628 / DIM_MARKET_PRODUCT_SCENE_ONCE / 产品-场景维表
    "product_scene": (
        ("op_time", "TEXT"),
        ("business_type", "VARCHAR(100)"),  # BUSINESS_TYPE 产品
        ("scene", "VARCHAR(255)"),          # SCENE 场景
    ),
    # F09629 / DIM_MARKET_PRODUCT_BUSINESS_ONCE / 产品策略维表
    # BUSINESS_NAME（策略）含【】内外文本，业务侧据此拆「资费名称 / 资费摘要」。
    "product_business": (
        ("op_time", "TEXT"),
        ("business_type", "VARCHAR(100)"),  # BUSINESS_TYPE 产品小类（样例可为空串）
        ("product_id", "VARCHAR(100)"),     # PRODUCT_ID 产品ID（诊断入口参数）
        ("business_name", "VARCHAR(500)"),  # BUSINESS_NAME 策略 / 产品名称
    ),
    # F09630 / DIM_MARKET_SCENE_QRHUASHU_ONCE / 场景-切入话术维表
    "scene_qrhuashu": (
        ("op_time", "TEXT"),
        ("scene", "VARCHAR(255)"),          # SCENE 场景
        ("qr_huashu", "TEXT"),              # QR_HUASHU 切入话术
    ),
    # F09631 / DIM_MARKET_PRODUCT_YXHUASHU_ONCE / 产品-营销话术维表
    "product_yxhuashu": (
        ("op_time", "TEXT"),
        ("business_type", "VARCHAR(100)"),  # BUSINESS_TYPE 产品
        ("yx_huashu", "TEXT"),              # YX_HUASHU 营销话术
    ),
    "product_wlhuashu": (
        ("op_time", "TEXT"),
        ("business_type", "VARCHAR(100)"),  # BUSINESS_TYPE 产品
        ("reject_reason", "VARCHAR(50)"),   # REJECT_REASON 拒绝原因
        ("wl_huashu", "TEXT"),              # WL_HUASHU 挽留话术
    ),
    # F09633 / DWD_MARKET_CONF_USER_SCENE_DS / 用户场景对应关系
    "conf_user_scene": (
        ("op_time", "TEXT"),
        ("user_id", "VARCHAR(50)"),         # USER_ID 用户编码
        ("product_no", "VARCHAR(50)"),      # PRODUCT_NO 用户号码（手机号）
        ("scene", "VARCHAR(50)"),           # SCENE 场景（单元格内可多值，拆分后一一对应）
        ("reason", "VARCHAR(1000)"),        # REASON 痛点诊断（与 SCENE 按位置配对）
    ),
    # F09635 / 标签ID-标签名（io_samples/README.md 标为 TAGID_TAGNAME）
    # xls 未给该表 DDL：末两列含义未确认，按 sftp_avl2db/README.md 的注册口径
    # 用占位名 unknown1 / unknown2；上游给出正式 DDL 后再复核列名。
    # 主键为 tag_name：诊断话术里出现的是标签名，按名反查 tag_id。
    "tagid_tagname": (
        ("op_time", "TEXT"),
        ("tag_id", "VARCHAR(64)"),          # 标签ID（32 位 MD5，= IOP 的 ITEMCODE）
        ("tag_name", "VARCHAR(255)"),       # 标签名称（主键）
        ("unknown1", "VARCHAR(100)"),       # 未确认列 1
        ("unknown2", "VARCHAR(100)"),       # 未确认列 2
    ),
}


# 主键注册：与 TABLE_SCHEMAS 同名同键，写入侧建表 / 幂等 / 跳过判定共用。复合主键保证
# upsert（ON CONFLICT DO UPDATE）幂等且不丢样例行；op_time 不进主键，同一主键只保留最新一条。
TABLE_PRIMARY_KEYS: Final[Mapping[str, tuple[str, ...]]] = {
    "opportunity_recall": ("product_no",),
    "loss_control": ("product_no",),
    # 一个场景一条优先级
    "scene_rank": ("scene",),
    # 一个产品下多个场景
    "product_scene": ("business_type", "scene"),
    # 输入产品ID 唯一映射到策略 / 产品
    "product_business": ("product_id",),
    # 按场景取切入话术
    "scene_qrhuashu": ("scene",),
    # 含话术全文：样例中同产品行内容重复会被幂等去重，且日后若一个产品配
    # 多条营销话术也不会互相覆盖
    "product_yxhuashu": ("business_type", "yx_huashu"),
    # 含话术全文：样例存在同一 (产品, 拒绝原因) 对应两条不同挽留话术
    # （说明文档 08「单元格多值按位置拆分」），避免被 REPLACE 丢行
    "product_wlhuashu": ("business_type", "reject_reason", "wl_huashu"),
    # 一个号码多个场景需多行；SCENE 与 REASON 按位置一一对应
    "conf_user_scene": ("product_no", "scene"),
    # 标签名唯一映射标签ID（= IOP 的 ITEMCODE）与两列未确认字段；
    # 诊断按「#标签名&」用 tag_name 反查 tag_id，故以标签名为主键
    "tagid_tagname": ("tag_name",),
}


def schema_of(name: str) -> tuple[tuple[str, str], ...]:
    """按名字取整份列定义。"""
    try:
        return TABLE_SCHEMAS[name]
    except KeyError as e:
        raise KeyError(f"未注册的表结构 {name!r}；请在 table_schemas.py 中定义") from e


def schema_columns(name: str) -> list[str]:
    """列名顺序（不含类型）"""
    return [c[0] for c in schema_of(name)]


def schema_primary_key(name: str) -> tuple[str, ...]:
    """按名字取主键列（复合主键为多列）"""
    try:
        return TABLE_PRIMARY_KEYS[name]
    except KeyError as e:
        raise KeyError(
            f"未注册的主键 {name!r}；请在 TABLE_PRIMARY_KEYS 中声明") from e


__all__ = [
    "TABLE_SCHEMAS", "TABLE_PRIMARY_KEYS", "schema_of", "schema_columns",
    "schema_primary_key",
]
