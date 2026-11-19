# -*- coding: utf-8 -*-
"""CRM 业务库的读写契约声明（纯静态、零 I/O、不读 .env）。

写入侧（``sftp_avl2db`` 建表 / 入库）与在线读取侧（``services/data_query.py``
组装 dict）共用这里的列顺序与查询声明，避免各维护一份。

``validate_specs(profiles)`` 是显式校验入口，由调用方在加载运行时配置后调用；
import 本模块不执行任何校验或 I/O。

契约 key 与任务名一致：``opportunity_recall`` / ``loss_control``。
"""
from __future__ import annotations

from typing import Any, Mapping

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

# key 与任务名一致；在线查询按任务名派发。
#
# mode 按「库数量 × 表数量」分成三种形式（多库多表不存在，不支持）：
#   multi_db_single_table  多库单表：db_map 按 product_id 选库名，每库一张表
#   single_db_multi_table  单库多表：单库按 crc32(product_no) % NUM_TABLES 分表
#   single_db_single_table 单库单表：单库一张固定表，需声明 table_name（当前无任务使用，预留）
#
# mode 是库表形式的唯一真源：TaskProfileConfig.mode 由 load_profiles() 从这里派生，
# 写入侧 build_layout() 与读取侧 _resolve() 都按同一组值分支；validate_specs()
# 会拦住 from_mapping() 手填导致的漂移。
#
#   db_map        : 多库单表专用，product_id → 库名（即 {库名}.db）；
#                   表名按固定约定 data_{库名}，与写入侧建表一致
#   table_name    : 单库单表专用，固定表名
#   where         : 查询条件列，值由 services/data_query.py 按列名从入参取
#   order_by      : 排序；None 表示不排序（仍 LIMIT 1）
#   db_map / product_ids : 供 main.py 做业务路由判断，避免 ID 散落在代码里
QUERY_SPECS = {
    "opportunity_recall": {
        "mode": "multi_db_single_table",
        "db_map": {
            "202608041615335173": "F09554",  # 号卡业务
            "202608041615335172": "F09555",  # 套餐业务
            "202608041615335174": "F09556",  # 宽带业务
        },
        "where": ["product_no"],
        "order_by": "op_time DESC",
    },
    "loss_control": {
        "mode": "single_db_multi_table",
        "product_ids": ["202609180955095402"],  # 控损专用
        "where": ["product_no"],
        "order_by": "op_time DESC",
    },
}

QUERY_MODES = (
    "multi_db_single_table",     # 多库单表
    "single_db_multi_table",     # 单库多表
    "single_db_single_table",    # 单库单表
)


def db_columns(task_name: str) -> list[str]:
    """返回列名顺序（不含类型）。"""
    return [c[0] for c in DB_SCHEMAS[task_name]]


def query_spec(task_name: str) -> dict[str, Any]:
    """返回该任务的在线查询声明。"""
    return QUERY_SPECS[task_name]


def validate_specs(profiles: Mapping[str, Any]) -> None:
    """配置漂移在启动时暴露，避免读写两侧悄悄用错表 / 列。

    运行时配置由参数 ``profiles`` 传入，因此其他项目用 ``from_mapping()``
    自建的 profile 也能接受同一套校验。

    :raises ValueError: key 不一致 / where 列不存在 / mode 非法 /
                       多库单表的库名不在前缀列表 / 单库多表缺分表声明 /
                       单库单表缺固定表名 / profile.mode 与 QUERY_SPECS.mode 不一致
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
        if mode == "multi_db_single_table":
            db_map = spec.get("db_map")
            if not db_map:
                raise ValueError(f"{name}: 多库单表模式需要 db_map（product_id → 库名）")
            for pid, db_name in db_map.items():
                if db_name not in prefixes:
                    raise ValueError(f"{name}: db_map[{pid}] 的库名 {db_name} 不在 FILE_PREFIXES 中")
        elif mode == "single_db_multi_table":
            if conf.num_tables <= 0 or not conf.table_name_prefix:
                raise ValueError(f"{name}: 单库多表模式需要 NUM_TABLES / TABLE_NAME_PREFIX")
        else:  # single_db_single_table
            if not spec.get("table_name"):
                raise ValueError(f"{name}: 单库单表模式需要 table_name（固定表名）")

        # 放在既有校验之后，保证「非法 mode / 缺分表配置」等错误仍先暴露。
        # load_profiles() 派生出的 profile 恒一致；这里拦的是 from_mapping()
        # 手填 mode 与 QUERY_SPECS 漂移的情况。
        if conf.mode != mode:
            raise ValueError(
                f"{name}: profile.mode={conf.mode} 与 QUERY_SPECS.mode={mode} 不一致；"
                "库表形式应以 QUERY_SPECS 为唯一真源")
