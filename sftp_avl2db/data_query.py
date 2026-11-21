# -*- coding: utf-8 -*-
"""在线业务库只读查询（统一入口，``sftp_avl2db`` 包内读取侧）。

一个 AVL 一个 db：``runtime/sqlite_db/{project}/{avl}.db``。``project`` 只在写入侧
决定输出目录；**在线查询只认 AVL 前缀**，直接定位到 ``{avl}.db``，业务侧的
``product_id`` 不进本模块。

表名恒由 AVL 名派生（``avl_specs.table_name()``），列顺序与查询声明来自
``avl_specs`` / ``table_schemas``，运行时路径来自 ``load_profiles()``。
库/表缺失、AVL 未注册或查询异常一律降级返回空结果，不会让 API 500。

本模块被显式 import 时才读包内 ``.env``；``import sftp_avl2db`` 不触发本模块，
包根仍保持零 I/O。

对外入口：

- ``query(avl, key)``      async，单条，按 ``AvlSpec.where`` 取 op_time 最新一条，返回 dict
- ``query_many(avl, *, where, limit=None)``  async，多行 / 多条件，返回 list[dict]
- ``query_in(avl, column, values, *, limit=None)``  ``query_many`` 的单列 IN 便捷形式

``query`` 的第二参数按 AVL 的 ``where`` 声明绑定：商机回捞 / 控损传手机号。
``query_many`` 的 ``where`` 是 ``{列名: 值}``，值为标量走 ``=``、为序列走 ``IN``，
多个键之间 AND；列名一律走 ``schema_columns()`` 白名单，值一律参数绑定，
不拼业务字符串进 SQL。排序固定取 ``AvlSpec.order_by``（静态声明），需要在
业务侧重排（如场景按 RN 优先级）时用 Python 排，不开放 ORDER BY 注入口。
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
    table = table_name(avl, _shard(conf, {"product_no": key}) if conf.sharded else None)
    columns = schema_columns(spec.schema)
    where_values = {col: key for col in spec.where}
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
