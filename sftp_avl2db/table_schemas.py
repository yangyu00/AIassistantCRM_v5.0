# -*- coding: utf-8 -*-
"""表结构定义：按名字注册，供多个 AVL 复用。

一个 AVL 对应一个 db；表结构按这里的 ``schema`` 名注册。同一业务的多个
AVL 可以引用同一个名字；不同表结构的 AVL 在这里加一份定义，再到
``avl_specs.AVL_SPECS`` 里指过来。

``TABLE_PRIMARY_KEYS`` 按同一 ``schema`` 名注册主键（支持复合主键），由
``config.load_profiles()`` 写进 profile，建表 DDL 与空主键跳过判定共用：
主键列自动补 ``NOT NULL``，``INSERT OR REPLACE`` 按主键幂等。

纯静态、零 I/O；写入侧建表 DDL 与读取侧 ``SELECT *`` 组装 dict 共用列顺序。

诊断新线 8 张表按「话术涉及表结构.xls」的物理 DDL 与 io_samples 的 AVL 列顺序
注册；业务口径见「营销话术字段取数逻辑说明」。两处口径不一致时以 xls 物理 DDL
为准（见 ``product_wlhuashu`` 注释），避免与上游文件列数、列名错位。
"""
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

    # ===================== 诊断新线（project = diagnosis） =====================
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
    # F09632 / DIM_MARKET_PRODUCT_WLHUASHU_ONCE / 产品-挽留话术维表
    # 物理列名按 xls DDL 注册为 WL_HUASHU（取数说明 09 记作 YX_HUASHU，
    # 业务侧如需别名另做映射，不改物理列名）。
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
    # xls 未给该表 DDL：列名按 5 列样例含义推定（MD5 标签ID / 标签名 /
    # 来源物理表 / 来源字段），上游若有正式 DDL 以正式 DDL 为准复核。
    "tagid_tagname": (
        ("op_time", "TEXT"),
        ("tag_id", "VARCHAR(64)"),          # 标签ID（32 位 MD5）
        ("tag_name", "VARCHAR(255)"),       # 标签名称
        ("table_name", "VARCHAR(100)"),     # 标签来源表
        ("column_name", "VARCHAR(100)"),    # 标签来源字段
    ),
}


# 主键注册：与 TABLE_SCHEMAS 同名同键，读写两侧共用。复合主键保证
# INSERT OR REPLACE 幂等且不丢样例行；op_time 不进主键（每日全量覆盖最新批次，
# 与商机回捞 / 控损「同一键只保留一条」的既有口径一致）。
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
    # 标签ID 唯一映射标签名与来源列
    "tagid_tagname": ("tag_id",),
}


def schema_of(name: str) -> tuple[tuple[str, str], ...]:
    """按名字取整份列定义；未注册直接报错，不静默回落。"""
    try:
        return TABLE_SCHEMAS[name]
    except KeyError as e:
        raise KeyError(f"未注册的表结构 {name!r}；请在 table_schemas.py 中定义") from e


def schema_columns(name: str) -> list[str]:
    """列名顺序（不含类型），写入 DDL 与读取侧组装 dict 共用。"""
    return [c[0] for c in schema_of(name)]


def schema_primary_key(name: str) -> tuple[str, ...]:
    """按名字取主键列（复合主键为多列）；未注册直接报错。"""
    try:
        return TABLE_PRIMARY_KEYS[name]
    except KeyError as e:
        raise KeyError(
            f"未注册的主键 {name!r}；请在 TABLE_PRIMARY_KEYS 中声明") from e


__all__ = [
    "TABLE_SCHEMAS", "TABLE_PRIMARY_KEYS", "schema_of", "schema_columns",
    "schema_primary_key",
]
