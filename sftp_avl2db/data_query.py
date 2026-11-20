# -*- coding: utf-8 -*-
"""在线业务库只读查询（统一入口，``sftp_avl2db`` 包内读取侧）。

一个 AVL 一个 db：``runtime/sqlite_db/{project}/{avl}.db``。``project`` 只在写入侧
决定输出目录；**在线查询只认 AVL 前缀**——``query(avl, telnum)`` 直接定位到
``{avl}.db``，``product_id`` 不进本模块。产品 ID 到 AVL 前缀的对应关系由业务侧
（``main.py`` 的路由表）维护，查库时把 AVL 前缀传进来即可。

表名恒由 AVL 名派生（``avl_specs.table_name()``），列顺序与查询声明来自
``avl_specs`` / ``table_schemas``，运行时路径来自 ``load_profiles()``。
库/表缺失、AVL 未注册或查询异常一律返回 ``{}``，不会让 API 500。

本模块被显式 import 时才读包内 ``.env``；``import sftp_avl2db`` 不触发本模块，
包根仍保持零 I/O。新增或调整 AVL 只需改 ``avl_specs.py`` / ``table_schemas.py``
与 ``config.py::_AVLS``，业务路由另改 ``main.py``。

对外只提供一个入口：
    query(avl, telnum)   async，按 AVL 前缀取 op_time 最新一条
"""
import os
import sqlite3

from .avl_specs import (
    AVL_SPECS, shard_index, spec_of, table_name, validate_specs,
)
from .config import load_profiles
from .table_schemas import schema_columns

# 在线侧启动时显式加载一次包内 .env（不传 runtime_dir，与写入侧默认同源），
# 并显式跑声明校验；读写两侧不会各算一套路径。
PROFILES = load_profiles()
validate_specs(PROFILES)


def _warn(msg):
    print(f"WARNING:: {msg}", flush=True)


def _error(msg):
    print(f"ERROR:: {msg}", flush=True)


def _resolve(avl, telnum):
    """由 AVL 前缀算出 (db_path, sql, columns, values)。

    一个 AVL 一个 db，没有项目派发、没有 product_id 映射、没有库表形式分支。
    """
    conf = PROFILES[avl]
    spec = spec_of(avl)
    db_path = conf.db_dir / f"{avl}.db"
    # 表名恒以 AVL 名为基准，写入侧 sink/router.py 用同一条规则。
    shard = (shard_index(telnum, conf.num_tables) if conf.sharded else None)
    table = table_name(avl, shard)

    columns = schema_columns(spec.schema)
    # 查询键恒为 product_no；其它条件属业务侧，不进本层（validate_specs 会拦）。
    where_values = {"product_no": telnum}
    values = [where_values[col] for col in spec.where]
    where_sql = " AND ".join(f"{col} = ?" for col in spec.where)
    order_sql = f" ORDER BY {spec.order_by}" if spec.order_by else ""
    sql = f"SELECT * FROM {table} WHERE {where_sql}{order_sql} LIMIT 1"
    return db_path, sql, columns, values


def _query(avl, telnum):
    db_path, sql, columns, values = _resolve(avl, telnum)

    if not os.path.isfile(db_path):
        _warn(f"数据库文件不存在: {db_path}")
        return {}

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


async def query(avl, telnum):
    """按 AVL 前缀查询一条业务记录；库/表缺失或异常时返回 {}。"""
    if avl not in AVL_SPECS:
        _warn(f"未注册的 AVL {avl}")
        return {}
    return _query(avl, telnum)
