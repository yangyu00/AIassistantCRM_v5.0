# -*- coding: utf-8 -*-
"""在线业务库只读查询（``services`` 读取侧，统一入口）。

写入侧 ``sftp_avl2db`` 只负责「AVL → SQLite」；**在线读取侧归本模块**。两侧
读写分离：写入侧不 import 本模块，本模块也不参与任何写入，只共用
``sftp_avl2db`` 的三份静态契约——

- ``avl_specs.AVL_SPECS``：AVL 集合、where / order_by 声明、分表口径
- ``table_schemas.TABLE_SCHEMAS``：列顺序（写入 DDL 与读取侧 dict 组装同源）
- ``config.load_profiles()``：由包内 ``.env`` 装配的库路径与分表数

一个 AVL 一个 db：``runtime/sqlite_db/{project}/{avl}.db``。``project`` 只在
写入侧决定输出目录；**在线查询只认 AVL 前缀**，业务侧的 ``product_id`` 不进
本模块。

表名恒由 AVL 名派生（``avl_specs.table_name()``），列顺序与查询声明来自
``avl_specs`` / ``table_schemas``，运行时路径来自 ``load_profiles()``。
库/表缺失、AVL 未注册或查询异常一律降级返回空结果，不会让 API 500。

本模块被显式 import / 首次查询时才装配 profiles（读包内 ``.env``）；
``import services`` 不触发本模块，包根仍保持零 I/O。

对外入口：

- ``query(avl, key)``      async，单条，按 ``AvlSpec.where`` 取 op_time 最新一条，返回 dict
- ``query_many(avl, *, where, limit=None)``  async，多行 / 多条件，返回 list[dict]
- ``query_in(avl, column, values, *, limit=None)``  ``query_many`` 的单列 IN 便捷形式

``query`` 的第二参数按 AVL 的 ``where`` 声明绑定：商机回捞 / 控损传手机号。
``query_many`` 的 ``where`` 是 ``{列名: 值}``，值为标量走 ``=``、为序列走 ``IN``，
多个键之间 AND；列名一律走 ``schema_columns()`` 白名单，值一律参数绑定，
不拼业务字符串进 SQL。排序固定取 ``AvlSpec.order_by``（静态声明），需要在
业务侧重排（如场景按 RN 优先级）时用 Python 排，不开放 ORDER BY 注入口。

分表 AVL 有两个：控损 ``F09591``、诊断用户场景 ``F09633``（千万级行）。
两者都以 ``product_no`` 为分表键，且在线查询键与之相同，因此 ``query`` /
``query_many`` 能精确定位到**单张**分表，不需要广播；``where`` 里必须带
**标量** ``product_no``，否则无法定位分表、直接降级空结果（不广播、不抛异常）。
``query_in`` 与不带 ``product_no`` 的 ``query_many`` 只对固定表有效。
"""
from __future__ import annotations

import os
import sqlite3

from sftp_avl2db.avl_specs import (
    AVL_SPECS, shard_index, spec_of, table_name, validate_specs,
)
from sftp_avl2db.config import load_profiles
from sftp_avl2db.table_schemas import schema_columns

# 读取侧启动时装配一次 profiles（不传 runtime_dir，与写入侧默认同源），
# 并显式跑声明校验；读写两侧不会各算一套路径。
# 离线自检可直接替换本模块的 PROFILES，把查询隔离到临时目录。
PROFILES = load_profiles()
validate_specs(PROFILES)


def configure(profiles) -> None:
    """替换读取侧的 profiles（仅离线自检 / 单测注入用）。

    注入的映射须来自 ``load_profiles()``，否则列顺序与分表口径会与写入侧漂移。
    """
    global PROFILES
    validate_specs(profiles)
    PROFILES = profiles


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
    return shard_index(key, conf.num_tables)


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
    spec = spec_of(avl)
    db_path = conf.db_dir / f"{avl}.db"
    where_values = {col: key for col in spec.where}
    # 分表 AVL 靠 where 里的 product_no 定位分表（F09591 / F09633 都是它）；
    # 固定表的 _shard 直接返回 None。where 声明与分表键由 validate_specs 保证一致。
    table = table_name(avl, _shard(conf, where_values))
    columns = schema_columns(spec.schema)
    values = [where_values[col] for col in spec.where]
    where_sql = " AND ".join(f"{col} = ?" for col in spec.where)
    order_sql = f" ORDER BY {spec.order_by}" if spec.order_by else ""
    sql = f"SELECT * FROM {table} WHERE {where_sql}{order_sql} LIMIT 1"
    return db_path, sql, columns, values


def _resolve_many(avl, where, limit=None):
    """多行查询：由 AVL 前缀与条件算出 (db_path, sql, columns, values)。"""
    conf = PROFILES[avl]
    spec = spec_of(avl)
    db_path = conf.db_dir / f"{avl}.db"
    table = table_name(avl, _shard(conf, where))
    columns = schema_columns(spec.schema)

    built = _build_where(columns, where)
    if built is None:
        return None
    clauses, values = built

    sql = f"SELECT * FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    # 排序常量来自 avl_specs 的静态声明，不接受外部字符串。
    if spec.order_by:
        sql += f" ORDER BY {spec.order_by}"
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
    if avl not in AVL_SPECS:
        _warn(f"未注册的 AVL {avl}")
        return {}
    return _query(avl, key)


async def query_many(avl, *, where, limit=None):
    """按 AVL 前缀查询多行；库/表缺失、空 IN 集合或异常时返回 []。

    :param where: ``{列名: 值}``，标量 → ``=``，序列 → ``IN``，多键 AND。
    :param limit: 可选行数上限（参数绑定）。
    """
    if avl not in AVL_SPECS:
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
