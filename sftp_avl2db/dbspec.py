# -*- coding: utf-8 -*-
"""读写共用的静态契约：列顺序、查询声明、库表形式常量与分表函数。

纯静态、零 I/O、不读 ``.env``；``validate_specs()`` 在启动时拦配置漂移。
"""
from __future__ import annotations

import zlib
from typing import Any, Final, Mapping


class QueryMode:
    """库表形式的唯一真源；写入侧与读取侧都引用这里的常量。"""

    MULTI_DB_SINGLE_TABLE: Final = "multi_db_single_table"
    SINGLE_DB_MULTI_TABLE: Final = "single_db_multi_table"
    SINGLE_DB_SINGLE_TABLE: Final = "single_db_single_table"


# 与 QUERY_SPECS / validate_specs() / _LAYOUT_BUILDERS / DEFAULT_SYNCHRONOUS
# 共用同一组值；多库多表不存在，不支持。
QUERY_MODES: Final = (
    QueryMode.MULTI_DB_SINGLE_TABLE,
    QueryMode.SINGLE_DB_MULTI_TABLE,
    QueryMode.SINGLE_DB_SINGLE_TABLE,
)

# product_no 在两种逻辑记录里都是第 3 列；解析、路由、校验共用这一个名字。
PRODUCT_NO_INDEX: Final = 2


def shard_index(product_no, num_tables: int) -> int:
    """分表号：读写两侧唯一口径（``zlib.crc32(...) % NUM_TABLES``）。

    空主键取字符串 ``"NULL"``；解析器已跳过空主键，属不可达分支但口径保留。
    """
    p_no = product_no if product_no else "NULL"
    return zlib.crc32(str(p_no).encode("utf-8")) % num_tables


# ---------------------------------------------------------------------------
# SQLite 表结构与在线查询声明
#
# 写入侧建表与读取侧组装 dict 共用这里的列顺序；库目录、文件前缀、分表数取自
# 运行时的 TaskProfileConfig，因此配置一处改动同时作用于读写两侧。
# ---------------------------------------------------------------------------
DB_SCHEMAS = {
    "opportunity_recall": [
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
    ],
    "loss_control": [
        ("op_time", "TEXT"),
        ("user_id", "TEXT"),
        ("product_no", "TEXT NOT NULL"),
        ("city_id", "TEXT"),
        ("strategy_name", "TEXT"),
    ],
}

# key 与任务名一致；在线查询按任务名派发。mode 取值只用 QueryMode 的常量。
#
#   db_map        : 多库单表专用，product_id → 库名（即 {库名}.db）；
#                   表名按固定约定 data_{库名}，与写入侧建表一致
#   table_name    : 单库单表专用，固定表名（当前无任务使用，预留）
#   where         : 查询条件列，值由 services/data_query.py 按列名从入参取
#   order_by      : 排序；None 表示不排序（仍 LIMIT 1）
#   db_map / product_ids : 供 main.py 做业务路由判断，避免 ID 散落在代码里
QUERY_SPECS = {
    "opportunity_recall": {
        "mode": QueryMode.MULTI_DB_SINGLE_TABLE,
        "db_map": {
            "202608041615335173": "F09554",  # 号卡业务
            "202608041615335172": "F09555",  # 套餐业务
            "202608041615335174": "F09556",  # 宽带业务
        },
        "where": ["product_no"],
        "order_by": "op_time DESC",
    },
    "loss_control": {
        "mode": QueryMode.SINGLE_DB_MULTI_TABLE,
        "product_ids": ["202609180955095402"],  # 控损专用
        "where": ["product_no"],
        "order_by": "op_time DESC",
    },
}


def db_columns(task_name: str) -> list[str]:
    """返回列名顺序（不含类型）。"""
    return [c[0] for c in DB_SCHEMAS[task_name]]


def query_spec(task_name: str) -> dict[str, Any]:
    """返回该任务的在线查询声明。"""
    return QUERY_SPECS[task_name]


def validate_specs(profiles: Mapping[str, Any]) -> None:
    """配置漂移在启动时暴露，避免读写两侧悄悄用错表 / 列。

    运行时配置由参数 ``profiles`` 传入，因此显式构造的 profile 也接受
    同一套校验。

    :raises ValueError: key 不一致 / where 列不存在 / mode 非法 /
                       多库单表的库名不在前缀列表 / 单库多表缺分表声明 /
                       单库单表缺固定表名 / profile.mode 或 table_name 漂移
    """
    names = set(profiles)
    if set(DB_SCHEMAS) != names or set(QUERY_SPECS) != names:
        raise ValueError("DB_SCHEMAS / QUERY_SPECS 的 key 必须与任务配置完全一致")

    for name, spec in QUERY_SPECS.items():
        conf = profiles[name]
        prefixes = set(conf.file_prefixes)
        cols = [c[0] for c in conf.schema]

        for col in spec["where"]:
            if col not in cols:
                raise ValueError(f"{name}: QUERY.where 列 {col} 不在 DB_SCHEMA 中")

        mode = spec["mode"]
        if mode not in QUERY_MODES:
            raise ValueError(
                f"{name}: 不支持的 QUERY.mode {mode}；仅支持 {QUERY_MODES}（多库多表不存在）"
            )
        if mode == QueryMode.MULTI_DB_SINGLE_TABLE:
            db_map = spec.get("db_map")
            if not db_map:
                raise ValueError(f"{name}: 多库单表模式需要 db_map（product_id → 库名）")
            for pid, db_name in db_map.items():
                if db_name not in prefixes:
                    raise ValueError(f"{name}: db_map[{pid}] 的库名 {db_name} 不在 FILE_PREFIXES 中")
        elif mode == QueryMode.SINGLE_DB_MULTI_TABLE:
            if conf.num_tables <= 0 or not conf.table_name_prefix:
                raise ValueError(f"{name}: 单库多表模式需要 NUM_TABLES / TABLE_NAME_PREFIX")
        else:  # QueryMode.SINGLE_DB_SINGLE_TABLE
            declared = spec.get("table_name")
            if not declared:
                raise ValueError(f"{name}: 单库单表模式需要 table_name（固定表名）")
            if getattr(conf, "table_name", "") != declared:
                raise ValueError(
                    f"{name}: profile.table_name 与 QUERY_SPECS.table_name 不一致；"
                    "固定表名应以 QUERY_SPECS 为唯一真源")

        # 放在既有校验之后，保证「非法 mode / 缺分表配置」等错误仍先暴露。
        # load_profiles() 派生出的 profile 恒一致；这里拦的是配置装配时
        # 手填 mode 与 QUERY_SPECS 漂移的情况。
        if conf.mode != mode:
            raise ValueError(
                f"{name}: profile.mode={conf.mode} 与 QUERY_SPECS.mode={mode} 不一致；"
                "库表形式应以 QUERY_SPECS 为唯一真源")
