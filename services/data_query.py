# -*- coding: utf-8 -*-
"""在线业务库只读查询（统一入口）。

两个 SFTP 任务的库目录、文件前缀、分表数、表结构与查询声明全部取自
``tasks.config.TASKS[...]``，本模块不含硬编码的表名/列名/路径。
新增或调整任务只需改 ``tasks/config.py`` 的 DB_SCHEMAS / QUERY_SPECS。

``QUERY_SPECS[任务名]["mode"]`` 决定库表形式，共三种：
多库单表（db_map 选库名）、单库多表（crc32 分表）、单库单表（table_name 固定表）；
多库多表不在支持范围内。本模块 ``_resolve()`` 按 mode 走对应分支。

对外提供：
    query(task_name, telnum, product_id=None)   按任务名派发
    get_opportunity_recall_latest / get_loss_control_latest
    OPPORTUNITY_RECALL_PRODUCT_IDS / LOSS_CONTROL_PRODUCT_IDS      供 main.py 做业务路由判断
"""
import os
import sqlite3
import zlib

from tasks.config import TASKS, QUERY_SPECS, db_dir

OPPORTUNITY_RECALL_PRODUCT_IDS = frozenset(QUERY_SPECS["opportunity_recall"]["db_map"])
LOSS_CONTROL_PRODUCT_IDS = frozenset(QUERY_SPECS["loss_control"]["product_ids"])


def _warn(msg):
    print(f"WARNING:: {msg}", flush=True)


def _error(msg):
    print(f"ERROR:: {msg}", flush=True)


def _resolve(task_name, product_no, product_id=None):
    """由配置算出 (db_path, table_name, where 值列表)；未映射时返回 None。"""
    conf = TASKS[task_name]
    spec = conf["QUERY"]
    qdir = db_dir(task_name)

    mode = spec["mode"]
    if mode == "multi_db_single_table":
        # 多库单表：db_map 的值是库名（{库名}.db），每库一张表 data_{库名}
        db_name = spec["db_map"].get(product_id)
        if not db_name:
            _warn(f"product_id [{product_id}] 未在 {task_name} db_map 中映射库名")
            return None
        db_path = qdir / f"{db_name}.db"
        table = f"data_{db_name}"
    elif mode == "single_db_multi_table":
        # 单库多表：单库按 crc32(product_no) 路由到 NUM_TABLES 张表之一
        db_path = qdir / f"{conf['FILE_PREFIXES'][0]}.db"
        p_no = product_no if product_no else "NULL"
        idx = zlib.crc32(p_no.encode("utf-8")) % conf["NUM_TABLES"]
        table = f"{conf['TABLE_NAME_PREFIX']}_{idx}"
    elif mode == "single_db_single_table":
        # 单库单表：单库一张固定表
        db_path = qdir / f"{conf['FILE_PREFIXES'][0]}.db"
        table = spec["table_name"]
    else:
        # 配置校验在 import 时已拒绝非法 mode；运行时仍降级，避免异常冒泡到业务侧
        _warn(f"{task_name} 不支持的 QUERY.mode {mode}")
        return None

    available = {"product_no": product_no, "product_id": product_id}
    values = [available[col] for col in spec["where"]]
    return db_path, table, values


def _query(task_name, product_no, product_id=None):
    resolved = _resolve(task_name, product_no, product_id)
    if resolved is None:
        return {}
    db_path, table, values = resolved

    if not os.path.isfile(db_path):
        _warn(f"数据库文件不存在: {db_path}")
        return {}

    conf = TASKS[task_name]
    spec = conf["QUERY"]
    columns = [c[0] for c in conf["DB_SCHEMA"]]
    where_sql = " AND ".join(f"{col} = ?" for col in spec["where"])
    order_sql = f" ORDER BY {spec['order_by']}" if spec["order_by"] else ""
    sql = f"SELECT * FROM {table} WHERE {where_sql}{order_sql} LIMIT 1"

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(sql, tuple(values)).fetchone()
        finally:
            conn.close()
        return dict(zip(columns, row)) if row else {}
    except Exception as e:
        _error(f"查询数据库失败: {e}")
        return {}


async def query(task_name, telnum, product_id=None):
    """按任务名查询一条业务记录；库/表/映射缺失或异常时返回 {}。"""
    if task_name not in QUERY_SPECS:
        _warn(f"未注册的查询任务 {task_name}")
        return {}
    return _query(task_name, telnum, product_id)


async def get_opportunity_recall_latest(telnum, product_id):
    """回捞商机：按 product_id 选库表，取 op_time 最新一条。"""
    return await query("opportunity_recall", telnum, product_id)


async def get_loss_control_latest(telnum):
    """控损专用：按手机号分表，取 op_time 最新一条。"""
    return await query("loss_control", telnum)
