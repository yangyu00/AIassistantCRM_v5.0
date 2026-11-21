# -*- coding: utf-8 -*-
"""在线业务库只读查询（``services`` 读取侧，统一入口）。

**读取侧的配置、表字段与查询声明全部由 ``services.db_config`` 独立维护**：
每个 AVL 对应哪个 db 文件、库里有哪些列、按什么键查、怎么排序、分表怎么
定位，都在那一处写明；本模块不 import 后台写入侧的任何代码或配置。两侧
唯一的交集是磁盘上的 db 文件。

一个 AVL 一个 db：``{AVL_DB_ROOT}/{project}/{avl}.db``。在线查询只认 AVL
前缀，业务侧的 ``product_id`` 不进本模块。表名恒由 AVL 名派生
（``db_config.table_name()``），列名 / 查询键 / 排序来自 ``DbProfile``。
库 / 表缺失、AVL 未注册或查询异常一律降级返回空结果，不会让 API 500。

本模块被显式 import / 首次查询时才装配读取配置（``services/.env`` 可选，
不存在时用 ``db_config`` 的代码内默认值）；``import services`` 不触发本模块。

对外入口：

- ``query(avl, key)``      async，单条，按 ``DbProfile.where`` 取 op_time 最新一条，返回 dict
- ``query_many(avl, *, where, limit=None)``  async，多行 / 多条件，返回 list[dict]
- ``query_in(avl, column, values, *, limit=None)``  ``query_many`` 的单列 IN 便捷形式

``query`` 的第二参数按声明的 ``where`` 绑定：商机回捞 / 控损传手机号。
``query_many`` 的 ``where`` 是 ``{列名: 值}``，值为标量走 ``=``、为序列走 ``IN``，
多个键之间 AND；列名一律走 ``DbProfile.columns`` 白名单，值一律参数绑定，
不拼业务字符串进 SQL。排序固定取静态声明，需要在业务侧重排（如场景按 RN
优先级）时用 Python 排，不开放 ORDER BY 注入口。

分表 AVL 有两个：控损 ``F09591``、诊断用户场景 ``F09633``（千万级行）。
两者都以 ``product_no`` 为分表键，且在线查询键与之相同，因此 ``query`` /
``query_many`` 能精确定位到**单张**分表，不需要广播；``where`` 里必须带
**标量** ``product_no``，否则无法定位分表、直接降级空结果（不广播、不抛异常）。
``query_in`` 与不带 ``product_no`` 的 ``query_many`` 只对固定表有效。
"""
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
    """替换读取侧配置（仅离线自检 / 单测注入用）。

    可传 ``db_config.ReadConfig``，也可传 ``{AVL: DbProfile}`` 映射；配置仍
    必须是 ``db_config`` 装配出来的对象，保证列顺序与分表口径是本层声明。
    """
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
    """分表 AVL 只能靠标量 product_no 定位分表；固定表返回 None。"""
    if not conf.sharded:
        return None
    key = where.get("product_no")
    if key is None or isinstance(key, (list, tuple, set, frozenset)):
        raise ValueError(
            "分表 AVL 的多行查询需要标量 product_no 定位分表；"
            "IN 查询请自行按分表键分组后逐个调用")
    return db_config.shard_index(key, conf.num_tables)


def _build_where(columns, where):
    """把 ``{列名: 标量|序列}`` 编译成 (SQL 片段, 绑定值)。

    返回 ``None`` 表示某个 IN 集合为空——整条查询必然无结果，直接短路，
    避免生成 ``IN ()`` 这种 SQLite 不接受语法。
    """
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
    """按 AVL 前缀查询一条业务记录；库/表缺失或异常时返回 {}。"""
    if avl not in PROFILES:
        _warn(f"未注册的 AVL {avl}")
        return {}
    return _query(avl, key)


async def query_many(avl, *, where, limit=None):
    """按 AVL 前缀查询多行；库/表缺失、空 IN 集合或异常时返回 []。

    :param where: ``{列名: 值}``，标量 → ``=``，序列 → ``IN``，多键 AND。
    :param limit: 可选行数上限（参数绑定）。
    """
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
