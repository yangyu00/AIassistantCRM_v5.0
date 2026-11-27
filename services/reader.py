"""在线业务库只读查询（``services`` 读取侧，统一入口）。"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta

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
            profiles=config,
            freshness_window_days=_CONFIG.freshness_window_days,
            freshness_window_by_project=_CONFIG.freshness_window_by_project)
    db_config.validate(_CONFIG)
    PROFILES = _CONFIG.profiles


def _warn(msg):
    print(f"WARNING:: {msg}", flush=True)


def _error(msg):
    print(f"ERROR:: {msg}", flush=True)


def _run_read(db_path, fn):
    """短连接执行一次只读查询；遇到 schema 变更时重建连接重试。

    写入侧对每日全量 AVL 用「同一 db 内 RENAME 双套表」做原子切换，切换
    事务提交瞬间会 bump schema cookie。恰好在切换前后打开的读连接上，第一条
    SELECT 可能报 ``SQLITE_SCHEMA / database schema has changed``；此时表本身
    是完整可读的，只是该连接的 prepared statement 缓存失效。重建连接即可
    读到已提交的完整一套（旧套或新套），不需要也不应该降级返回空。

    只对这一类 OperationalError 重试；其它异常照旧交给调用方降级处理。
    """
    last = None
    for _ in range(3):
        conn = None
        try:
            conn = sqlite3.connect(str(db_path))
            return fn(conn)
        except sqlite3.OperationalError as e:
            last = e
            if "schema" not in str(e).lower():
                raise
        finally:
            if conn is not None:
                conn.close()
    raise last  # type: ignore[misc]


def _latest_op_time(conn, avl, window):
    """同连接读写入侧登记的批次水位。

    元数据表缺失 / 无行 / 该业务窗口关闭都返回 ``None``，此时不做批次过滤。
    """
    if window <= 0:
        return None
    try:
        row = conn.execute(
            f"SELECT latest_op_time FROM {db_config.AVL_META_TABLE} "
            "WHERE avl = ?", (avl,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def _cutoff(latest_op_time, project=None):
    """窗口下界 = 最新数据日期 - (N-1) 天，N 按业务（project）取。

    N 表示「保留最近 N 个 op_time 日期」：N=2、最新 21 日 → 下界 20 日。
    ``project=None`` 时用全局回退窗口（离线自检直调本函数的口径）。
    以数据里的 ``op_time`` 为基准；日期文本不是 YYYY-MM-DD 时降级不做过滤，避免误杀。
    """
    window = _CONFIG.freshness_window_days_for(project)
    if window <= 0 or not latest_op_time:
        return None
    try:
        day = datetime.strptime(str(latest_op_time)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (day - timedelta(days=window - 1)).isoformat()


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


def _resolve(avl, key, cutoff=None):
    """单条查询：倒序取最新一条；有水位时先按窗口滤掉过时批次。"""
    conf = PROFILES[avl]
    db_path = conf.db_path
    where_values = {col: key for col in conf.where}
    # 分表 AVL 靠 where 里的 product_no 定位分表（F09591 / F09633 都是它）；
    # 固定表的 _shard 直接返回 None。分表键与查询键由 db_config.validate() 保证一致。
    table = db_config.table_name(avl, _shard(conf, where_values))
    columns = list(conf.columns)
    clauses = [f"{col} = ?" for col in conf.where]
    values = [where_values[col] for col in conf.where]
    if cutoff:
        clauses.append(f"{db_config.TIME_COLUMN} >= ?")
        values.append(cutoff)
    where_sql = " AND ".join(clauses)
    order_sql = f" ORDER BY {conf.order_by}" if conf.order_by else ""
    sql = f"SELECT * FROM {table} WHERE {where_sql}{order_sql} LIMIT 1"
    return db_path, sql, columns, values


def _resolve_many(avl, where, limit=None, cutoff=None):
    """多行查询：有水位时只返回窗口内 ``op_time`` 最大的那一批。"""
    conf = PROFILES[avl]
    db_path = conf.db_path
    table = db_config.table_name(avl, _shard(conf, where))
    columns = list(conf.columns)

    built = _build_where(columns, where)
    if built is None:
        return None
    clauses, values = built

    if cutoff:
        # 先框窗口，再在结果集内收敛到最新批次；CTE 避免重复拼接 where 条件。
        clauses.append(f"{db_config.TIME_COLUMN} >= ?")
        values.append(cutoff)
        inner = " AND ".join(clauses) if clauses else "1"
        t = db_config.TIME_COLUMN
        sql = (f"WITH base AS (SELECT * FROM {table} WHERE {inner}) "
               f"SELECT * FROM base WHERE {t} = (SELECT MAX({t}) FROM base)")
    else:
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
    conf = PROFILES[avl]
    db_path = conf.db_path
    if not os.path.isfile(db_path):
        _warn(f"数据库文件不存在: {db_path}")
        return {}
    def once(conn):
        window = _CONFIG.freshness_window_days_for(conf.project)
        cutoff = _cutoff(_latest_op_time(conn, avl, window), conf.project)
        _, sql, columns, values = _resolve(avl, key, cutoff=cutoff)
        row = conn.execute(sql, tuple(values)).fetchone()
        return dict(zip(columns, row)) if row else {}

    try:
        return _run_read(db_path, once)
    except Exception as e:
        _error(f"查询数据库失败: {e}")
        return {}


def _query_many(avl, where, limit=None):
    conf = PROFILES[avl]
    db_path = conf.db_path
    if not os.path.isfile(db_path):
        _warn(f"数据库文件不存在: {db_path}")
        return []
    def once(conn):
        window = _CONFIG.freshness_window_days_for(conf.project)
        cutoff = _cutoff(_latest_op_time(conn, avl, window), conf.project)
        resolved = _resolve_many(avl, where, limit=limit, cutoff=cutoff)
        if resolved is None:
            return []
        _, sql, columns, values = resolved
        rows = conn.execute(sql, tuple(values)).fetchall()
        return [dict(zip(columns, row)) for row in rows]

    try:
        return _run_read(db_path, once)
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
