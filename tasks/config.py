# -*- coding: utf-8 -*-
"""tasks 独立配置：读取项目根目录 .env，并提供 SQLite 库目录给在线查询复用。

不依赖项目总 config.py / config.yaml。
优先级：系统环境变量 > .env；可用 TASKS_ENV_FILE 指定其他 .env 路径。
"""
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_QUOTED_RE = re.compile(r"""(?P<q>["'])(?P<v>.*?)(?<!\\)(?P=q)\s*(?:#.*)?$""", re.S)


def _unescape(value: str, quote: str) -> str:
    if quote == '"':
        return value.replace(r"\n", "\n").replace(r"\t", "\t").replace(r'\"', '"').replace(r"\\", "\\")
    return value.replace(r"\'", "'")


def _expand(value: str, parsed: dict) -> str:
    def repl(m):
        name = m.group(1) or m.group(2)
        return os.environ.get(name, parsed.get(name, m.group(0)))
    return _VAR_RE.sub(repl, value)


def parse_dotenv(path) -> dict:
    """解析 .env：支持 # 注释、行内注释、export、单/双引号与 ${VAR} 插值。"""
    parsed = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if not key:
            continue
        m = _QUOTED_RE.match(val)
        if m:
            val = _unescape(m.group("v"), m.group("q"))
        else:
            val = re.sub(r"\s+#.*$", "", val).rstrip()
        parsed[key] = _expand(val, parsed)
    return parsed


def load_values():
    path = Path(os.environ.get("TASKS_ENV_FILE") or DEFAULT_ENV_PATH)
    values = dict(os.environ)
    if path.is_file():
        for k, v in parse_dotenv(path).items():
            values.setdefault(k, v)
    elif not all(k in values for k in ("SFTP_HOST", "SFTP_PASSWORD")):
        raise FileNotFoundError(f"未找到 .env：{path}；也可通过系统环境变量提供配置")
    return values


V = load_values()


def _str(key):
    if not V.get(key):
        raise RuntimeError(f"缺少配置项 {key}（.env: {os.environ.get('TASKS_ENV_FILE') or DEFAULT_ENV_PATH}）")
    return V[key]


def _int(key):
    val = int(_str(key))
    if val <= 0:
        raise ValueError(f"{key} 必须为正整数，当前为 {val}")
    return val


def _list(key):
    return [x.strip() for x in _str(key).split(",") if x.strip()]


SFTP = {
    "host": _str("SFTP_HOST"),
    "port": _int("SFTP_PORT"),
    "username": _str("SFTP_USERNAME"),
    "password": _str("SFTP_PASSWORD"),
    "remote_path": _str("SFTP_REMOTE_PATH"),
}


def _task(prefix):
    return {
        "SFTP": SFTP,
        "FILE_PREFIXES": _list(f"{prefix}_FILE_PREFIXES"),
        "BATCH_SIZE": _int(f"{prefix}_BATCH_SIZE"),
        "DATA_EXPIRE_DAYS": _int(f"{prefix}_DATA_EXPIRE_DAYS"),
        "LOCAL_FILE_EXPIRE_DAYS": _int(f"{prefix}_LOCAL_FILE_EXPIRE_DAYS"),
        "POLL_INTERVAL_SECONDS": _int(f"{prefix}_POLL_INTERVAL_SECONDS"),
    }


def _multi_table_task(prefix):
    conf = _task(prefix)
    conf["NUM_TABLES"] = _int(f"{prefix}_NUM_TABLES")
    conf["TABLE_NAME_PREFIX"] = _str(f"{prefix}_TABLE_NAME_PREFIX")
    return conf


TASKS = {
    "opportunity_recall": _task("OPPORTUNITY_RECALL"),
    "loss_control": _multi_table_task("LOSS_CONTROL"),
    "customer_diagnosis": _multi_table_task("CUSTOMER_DIAGNOSIS"),
}

# ---------------------------------------------------------------------------
# SQLite 表结构与在线查询声明
#
# 写入侧（3 个 task_*.py 建表）与读取侧（services/data_query.py 组装 dict）
# 共用这里的列顺序；库目录、文件前缀、分表数取自对应 TASKS[...]，
# 因此 .env 一处改动同时作用于读写两侧，不再各维护一份。
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
    "customer_diagnosis": [
        ("op_time", "TEXT"),
        ("user_id", "TEXT"),
        ("product_no", "TEXT NOT NULL"),
        ("product_id", "TEXT NOT NULL"),
        ("strategy_script", "TEXT"),
    ],
}

# key 与 TASKS 一致；在线查询按任务名派发。
#
# mode 按「库数量 × 表数量」分成三种形式（多库多表不存在，不支持）：
#   multi_db_single_table  多库单表：db_map 按 product_id 选库名，每库一张表
#   single_db_multi_table  单库多表：单库按 crc32(product_no) % NUM_TABLES 分表
#   single_db_single_table 单库单表：单库一张固定表，需声明 table_name（当前无任务使用，预留）
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
    "customer_diagnosis": {
        "mode": "single_db_multi_table",
        "where": ["product_no", "product_id"],
        "order_by": None,
    },
}

QUERY_MODES = (
    "multi_db_single_table",     # 多库单表
    "single_db_multi_table",     # 单库多表
    "single_db_single_table",    # 单库单表
)


def _validate_specs():
    """配置漂移在启动时暴露，避免读写两侧悄悄用错表/列。"""
    if set(DB_SCHEMAS) != set(TASKS) or set(QUERY_SPECS) != set(TASKS):
        raise ValueError("DB_SCHEMAS / QUERY_SPECS 的 key 必须与 TASKS 完全一致")
    for name, spec in QUERY_SPECS.items():
        conf = TASKS[name]
        cols = [c[0] for c in DB_SCHEMAS[name]]
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
            prefixes = set(conf["FILE_PREFIXES"])
            for pid, db_name in db_map.items():
                if db_name not in prefixes:
                    raise ValueError(f"{name}: db_map[{pid}] 的库名 {db_name} 不在 FILE_PREFIXES 中")
        elif mode == "single_db_multi_table":
            if "NUM_TABLES" not in conf or "TABLE_NAME_PREFIX" not in conf:
                raise ValueError(f"{name}: 单库多表模式需要 NUM_TABLES / TABLE_NAME_PREFIX")
        else:  # single_db_single_table
            if not spec.get("table_name"):
                raise ValueError(f"{name}: 单库单表模式需要 table_name（固定表名）")


_validate_specs()

for _name, _spec in QUERY_SPECS.items():
    TASKS[_name]["DB_SCHEMA"] = DB_SCHEMAS[_name]
    TASKS[_name]["QUERY"] = _spec

# 各任务写入 SQLite 的目录，与 base_task 子类的 DB_DIR 约定一致。
# 在线查询 services/data_query.py 也读这里，保证读写路径只有一处定义。
DB_ROOT = PROJECT_ROOT / "tasks" / "sqlite_db"


def db_dir(task_name: str) -> Path:
    return DB_ROOT / task_name
