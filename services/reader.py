# -*- coding: utf-8 -*-
"""在线业务库只读查询（``services`` 读取侧，统一入口）。"""
from __future__ import annotations

import os
import sqlite3

from services import db_config

# 读取侧启动时装配一次自己的配置；离线自检可直接替换本模块的配置快照，
# 把查询隔离到临时目录。
_CONFIG = db_config.load()
db_config.validate(_CONFIG)
PROFILES = _CONFIG.profiles


def configure(config) -> None:
    """替换读取侧配置（仅离线自检 / 单测注入用）。"""
    global _CONFIG, PROFILES
    if isinstance(config, db_config.ReadConfig):
        _CONFIG = config
    else:
        _CONFIG = db_config.ReadConfig(
            avl_db_root=db_config.DEFAULT_AVL_DB_ROOT,
            dimensions_db=db_config.default_dimensions_db(),
            profiles=config)
    db_config.validate(_CONFIG)
    PROFILES = _CONFIG.profiles


def _warn(msg):
    print(f"WARNING:: {msg}", flush=True)


def _error(msg):
    print(f"ERROR:: {msg}", flush=True)


def _shard(conf, where):
    """分表 AVL 只能靠标量 product_no 定位分表。"""
    if not conf.sharded:
        return None
    key = where.get("product_no")
    if key is None or isinstance(key, (list, tuple, set, frozenset)):
        raise ValueError(
            "分表 AVL 的多行查询需要标量 product_no 定位分表；"
            "IN 查询请自行按分表键分组后逐个调用")
    return db_config.shard_index(key, conf.num_tables)


def _build_where(columns, where):
    """把 ``{列名: 标量|序列}`` 编译成 (SQL 片段, 绑定值)。"""
    clauses, values = [], []
    for col, val in where.items():
        if col not in columns:
            raise ValueError(f"查询列 {col!r} 不在表结构列中")
        if isinstance(val, (list, tuple, set, frozenset)):
            items = [v for v in val if v is not None]
            if not items:
                return None
            clauses.append(f"{col} IN ({', '.join('?' for _ in items)})")
            values.extend(items)
        else:
            clauses.append(f"{col} = ?")
            values.append(val)
    return clauses, values


def _resolve(avl, key):
    """单条查询：由 AVL 前缀算出 (db_path, sql, columns, values)。"""
    conf = PROFILES[avl]
    db_path = conf.db_path
    where_values = {col: key for col in conf.where}
    # 分表 AVL 靠 where 里的 product_no 定位分表（F09591 / F09633 都是它）；
    # 固定表的 _shard 直接返回 None。分表键与查询键由 db_config.validate() 保证一致。
    table = db_config.table_name(avl, _shard(conf, where_values))
    columns = list(conf.columns)
    values = [where_values[col] for col in conf.where]
    where_sql = " AND ".join(f"{col} = ?" for col in conf.where)
    order_sql = f" ORDER BY {conf.order_by}" if conf.order_by else ""
    sql = f"SELECT * FROM {table} WHERE {where_sql}{order_sql} LIMIT 1"
    return db_path, sql, columns, values


def _resolve_many(avl, where, limit=None):
    """多行查询：由 AVL 前缀与条件算出 (db_path, sql, columns, values)。"""
    conf = PROFILES[avl]
    db_path = conf.db_path
    table = db_config.table_name(avl, _shard(conf, where))
    columns = list(conf.columns)

    built = _build_where(columns, where)
    if built is None:
        return None
    clauses, values = built

    sql = f"SELECT * FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    # 排序常量来自 db_config 的静态声明，不接受外部字符串。
    if conf.order_by:
        sql += f" ORDER BY {conf.order_by}"
    if limit is not None:
        sql += " LIMIT ?"
        values.append(int(limit))
    return db_path, sql, columns, values


def _query(avl, key):
    db_path, sql, columns, values = _resolve(avl, key)
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


def _query_many(avl, where, limit=None):
    resolved = _resolve_many(avl, where, limit=limit)
    if resolved is None:
        return []
    db_path, sql, columns, values = resolved
    if not os.path.isfile(db_path):
        _warn(f"数据库文件不存在: {db_path}")
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(sql, tuple(values)).fetchall()
        finally:
            conn.close()
        return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        _error(f"查询数据库失败: {e}")
        return []


async def query(avl, key):
    """按 AVL 前缀查询一条业务记录。"""
    if avl not in PROFILES:
        _warn(f"未注册的 AVL {avl}")
        return {}
    return _query(avl, key)


async def query_many(avl, *, where, limit=None):
    """按 AVL 前缀查询多行。"""
    if avl not in PROFILES:
        _warn(f"未注册的 AVL {avl}")
        return []
    try:
        return _query_many(avl, where, limit=limit)
    except (ValueError, KeyError) as e:
        _error(f"查询条件非法: {e}")
        return []


async def query_in(avl, column, values, *, limit=None):
    """单列 IN 的便捷形式，等价于 ``query_many(avl, where={column: values})``。"""
    return await query_many(avl, where={column: list(values)}, limit=limit)


__all__ = ["PROFILES", "configure", "query", "query_many", "query_in"]
