# -*- coding: utf-8 -*-
"""在线业务侧**独立维护**的各 db 文件读取配置与静态声明。

``services`` 读取侧要读哪些库文件、库里有哪些表 / 列、按什么键查、怎么排序、
分表怎么定位，全部在本文件里自己声明；**不 import 任何写入侧包**
（``sftp_avl2db`` / ``local_excel2db``）。写入侧只负责把数据写进磁盘，两侧
唯一的交集是磁盘上的 db 文件，没有代码或配置模块互相引用。

表字段在写入侧也有一份（建表 DDL 用），这里刻意冗余再写一遍：读在线侧代码时
不用跳到别的包，就能看清 ``SELECT`` 回来的列顺序与 dict 的 key。字段声明重复
是本次解耦刻意接受的成本。

分表口径 ``zlib.crc32(product_no) % NUM_TABLES`` 与表名 ``{AVL}_{分表号}`` 是
物理数据布局，写入侧也按它落库，所以这里独立实现同一算法。改分表数时两侧配置
必须同步，并重灌对应 AVL（见 ``services/README.md``）。

配置来源（优先级从高到低）：

1. ``load()`` 的显式参数（离线自检 / 单测注入）；
2. 包内 ``services/.env``（``KEY=VALUE``，从 ``.env.example`` 复制）；
3. 本文件顶部的默认值常量。

``import services.db_config`` 零 I/O；只有调用 ``load()`` 才会读 ``.env``。
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Mapping

MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parent
ENV_PATH = MODULE_DIR / ".env"

# ---- 默认库文件位置（按 __file__ 定位，与启动 CWD 无关）--------------------
# 写入侧把业务库产出到 sftp_avl2db/runtime/sqlite_db/{project}/{avl}.db；
# 在线侧只是「按自己声明的路径去读同一份文件」，不引用写入侧的配置代码。
DEFAULT_AVL_DB_ROOT: Final = (PROJECT_ROOT / "sftp_avl2db" / "runtime"
                              / "sqlite_db")
DEFAULT_DIMENSIONS_DB: Final = (PROJECT_ROOT / "local_excel2db" / "runtime"
                                / "sqlite_db" / "dimensions.db")

# ---- 分表数：与写入侧 .env 同名键，独立声明 -------------------------------
NUM_TABLES_ENV_KEYS: Final = {
    "F09591": "LOSS_CONTROL_NUM_TABLES",   # 控损
    "F09633": "F09633_NUM_TABLES",         # 诊断·用户场景对应关系
}
NUM_TABLES_DEFAULTS: Final = {"F09591": 128, "F09633": 128}

# ---------------------------------------------------------------------------
# 一、各业务库的表字段（与写入侧建表 DDL 对齐；冗余声明便于在线侧独立理解）
#     key = schema 名；值 = ((列名, 类型), ...)，顺序即 SELECT * 的列顺序
# ---------------------------------------------------------------------------
TABLE_SCHEMAS: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    # 商机回捞：F09554 / F09555 / F09556 共用
    "opportunity_recall": (
        ("op_time", "TEXT"),            # 数据日期
        ("call_time", "TEXT"),          # 通话时间
        ("product_no", "TEXT NOT NULL"),# 手机号（主键 / 查询键）
        ("type_dl", "VARCHAR(50)"),
        ("type_xl", "VARCHAR(50)"),
        ("content_abstract", "TEXT"),   # 通话总结
        ("cust_demand", "TEXT"),        # 用户需求
        ("rec_strategy", "TEXT"),       # 已推荐政策
        ("reject_reason", "TEXT"),      # 拒绝原因
        ("reason_note", "TEXT"),        # 具体表述
        ("refer_strategy", "TEXT"),
        ("city_id", "INTEGER"),
    ),
    # 控损：F09591，按 product_no 分 128 张表
    "loss_control": (
        ("op_time", "TEXT"),
        ("user_id", "TEXT"),
        ("product_no", "TEXT NOT NULL"),
        ("city_id", "TEXT"),
        ("strategy_name", "TEXT"),
    ),
    # 诊断新线 8 张表：一个 AVL 一个 db，列顺序 = AVL 制表符分隔顺序
    "scene_rank": (
        ("op_time", "TEXT"), ("scene", "VARCHAR(255)"), ("rn", "INTEGER"),
    ),
    "product_scene": (
        ("op_time", "TEXT"), ("business_type", "VARCHAR(100)"),
        ("scene", "VARCHAR(255)"),
    ),
    "product_business": (
        ("op_time", "TEXT"), ("business_type", "VARCHAR(100)"),
        ("product_id", "VARCHAR(100)"), ("business_name", "VARCHAR(500)"),
    ),
    "scene_qrhuashu": (
        ("op_time", "TEXT"), ("scene", "VARCHAR(255)"), ("qr_huashu", "TEXT"),
    ),
    "product_yxhuashu": (
        ("op_time", "TEXT"), ("business_type", "VARCHAR(100)"),
        ("yx_huashu", "TEXT"),
    ),
    "product_wlhuashu": (
        ("op_time", "TEXT"), ("business_type", "VARCHAR(100)"),
        ("reject_reason", "VARCHAR(50)"), ("wl_huashu", "TEXT"),
    ),
    "conf_user_scene": (
        ("op_time", "TEXT"), ("user_id", "VARCHAR(50)"),
        ("product_no", "VARCHAR(50)"), ("scene", "VARCHAR(50)"),
        ("reason", "VARCHAR(1000)"),
    ),
    "tagid_tagname": (
        ("op_time", "TEXT"), ("tag_id", "VARCHAR(64)"),
        ("tag_name", "VARCHAR(255)"), ("unknown1", "VARCHAR(100)"),
        ("unknown2", "VARCHAR(100)"),
    ),
}

# 主键声明（写入侧同样有一份）：在线侧只用于契约自检，不参与建表。
TABLE_PRIMARY_KEYS: Final[Mapping[str, tuple[str, ...]]] = {
    "opportunity_recall": ("product_no",),
    "loss_control": ("product_no",),
    "scene_rank": ("scene",),
    "product_scene": ("business_type", "scene"),
    "product_business": ("product_id",),
    "scene_qrhuashu": ("scene",),
    "product_yxhuashu": ("business_type", "yx_huashu"),
    "product_wlhuashu": ("business_type", "reject_reason", "wl_huashu"),
    "conf_user_scene": ("product_no", "scene"),
    "tagid_tagname": ("tag_id",),
}


# ---------------------------------------------------------------------------
# 二、每个 AVL 对应一个 db 文件 + 一份在线查询声明
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AvlDbSpec:
    """一个 AVL 的在线侧读取声明（不含任何写入侧信息）。"""

    name: str                       # AVL 名，也是库内固定表名
    project: str                    # db 文件所在子目录
    db_file: str                    # 相对 AVL_DB_ROOT 的库文件路径
    schema: str                     # TABLE_SCHEMAS 的 key
    where: tuple[str, ...]          # 单条查询的默认键（query(avl, key) 绑定）
    order_by: str | None            # 固定排序声明，不接受外部字符串
    sharded: bool = False           # 库内是否按 product_no 分表
    num_tables_env_key: str | None = None   # 分表数在 .env 里的键名


AVL_DB_SPECS: Final[Mapping[str, AvlDbSpec]] = {
    "F09554": AvlDbSpec("F09554", "opportunity_recall",
                        "opportunity_recall/F09554.db", "opportunity_recall",
                        ("product_no",), "op_time DESC"),
    "F09555": AvlDbSpec("F09555", "opportunity_recall",
                        "opportunity_recall/F09555.db", "opportunity_recall",
                        ("product_no",), "op_time DESC"),
    "F09556": AvlDbSpec("F09556", "opportunity_recall",
                        "opportunity_recall/F09556.db", "opportunity_recall",
                        ("product_no",), "op_time DESC"),
    "F09591": AvlDbSpec("F09591", "loss_control", "loss_control/F09591.db",
                        "loss_control", ("product_no",), "op_time DESC",
                        sharded=True, num_tables_env_key="LOSS_CONTROL_NUM_TABLES"),
    "F09627": AvlDbSpec("F09627", "diagnosis", "diagnosis/F09627.db",
                        "scene_rank", ("scene",), "op_time DESC"),
    "F09628": AvlDbSpec("F09628", "diagnosis", "diagnosis/F09628.db",
                        "product_scene", ("business_type",), "op_time DESC"),
    "F09629": AvlDbSpec("F09629", "diagnosis", "diagnosis/F09629.db",
                        "product_business", ("product_id",), "op_time DESC"),
    "F09630": AvlDbSpec("F09630", "diagnosis", "diagnosis/F09630.db",
                        "scene_qrhuashu", ("scene",), "op_time DESC"),
    "F09631": AvlDbSpec("F09631", "diagnosis", "diagnosis/F09631.db",
                        "product_yxhuashu", ("business_type",), "op_time DESC"),
    "F09632": AvlDbSpec("F09632", "diagnosis", "diagnosis/F09632.db",
                        "product_wlhuashu", ("business_type",), "op_time DESC"),
    "F09633": AvlDbSpec("F09633", "diagnosis", "diagnosis/F09633.db",
                        "conf_user_scene", ("product_no",), "op_time DESC",
                        sharded=True, num_tables_env_key="F09633_NUM_TABLES"),
    "F09635": AvlDbSpec("F09635", "diagnosis", "diagnosis/F09635.db",
                        "tagid_tagname", ("tag_id",), "op_time DESC"),
}


# ---------------------------------------------------------------------------
# 三、卖点维表 dimensions.db（在线侧自己声明，不读写入侧 registry.json）
# ---------------------------------------------------------------------------
SELLING_POINTS_TABLE: Final = "selling_points"
SELLING_POINTS_PRIMARY_KEY: Final = ("product_id",)
SELLING_POINTS_REQUIRED_COLUMNS: Final = ("product_id", "label_id")
SELLING_POINTS_OPTIONAL_COLUMNS: Final = (
    "product_name", "selling_point", "fallback_script", "match_result")
# 「标签0…标签27」展开成 tag_0…tag_27，与 match_result 里的场景 ID 按下标对应
SELLING_POINTS_TAG_COLUMNS: Final = tuple(f"tag_{i}" for i in range(28))
# SELECT 列顺序：必需 → 可选 → 动态标签列；缺列的库文件会降级返回 {}
SELLING_POINTS_SELECT_COLUMNS: Final = (
    SELLING_POINTS_REQUIRED_COLUMNS + SELLING_POINTS_OPTIONAL_COLUMNS
    + SELLING_POINTS_TAG_COLUMNS)
# 完整列定义（含类型），仅用于离线自检按同一份声明建临时库
SELLING_POINTS_SCHEMA: Final = tuple(
    (c, "TEXT") for c in (
        "product_id", "product_name", "selling_point", "label_id",
        "fallback_script", "match_result", "product_category",
        "product_subcategory") + SELLING_POINTS_TAG_COLUMNS)


# ---------------------------------------------------------------------------
# 四、分表口径与表名（物理布局；写入侧独立实现了同样的规则）
# ---------------------------------------------------------------------------
def shard_index(product_no, num_tables: int) -> int:
    """分表号：``zlib.crc32(str(product_no)) % NUM_TABLES``。

    空主键取字符串 ``"NULL"``（解析器已跳过空主键，属不可达分支但口径保留）。
    """
    p_no = product_no if product_no else "NULL"
    return zlib.crc32(str(p_no).encode("utf-8")) % num_tables


def table_name(base: str, shard: int | None = None) -> str:
    """库内表名：固定表 = AVL 名；分表 = ``{AVL 名}_{分表号}``。"""
    return base if shard is None else f"{base}_{shard}"


# ---------------------------------------------------------------------------
# 五、装配：把静态声明 + .env / 显式参数变成一份只读配置
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DbProfile:
    """一个 AVL 的运行时读取配置（不可变）。"""

    name: str
    project: str
    db_file: str
    db_root: Path
    columns: tuple[str, ...]                 # SELECT * 的列顺序 / dict 的 key
    schema: tuple[tuple[str, str], ...]      # 带类型的列声明（冗余，便于对照）
    primary_key: tuple[str, ...]
    where: tuple[str, ...]
    order_by: str | None
    sharded: bool
    num_tables: int                          # 分表数；固定表为 0

    @property
    def db_path(self) -> Path:
        return self.db_root / self.db_file

    @property
    def db_dir(self) -> Path:
        return self.db_path.parent


@dataclass(frozen=True)
class ReadConfig:
    """在线侧读取配置的完整快照。"""

    avl_db_root: Path
    dimensions_db: Path
    profiles: Mapping[str, DbProfile]


def _read_env(path) -> dict[str, str]:
    """读 KEY=VALUE；文件不存在返回空 dict（回落到代码内默认值）。"""
    p = Path(path)
    if not p.is_file():
        return {}
    values = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key:
            values[key] = value
    return values


def _positive_int(raw: str, key: str) -> int:
    try:
        n = int(raw)
    except ValueError as e:
        raise ValueError(f"{key} 必须是正整数，当前为 {raw!r}") from e
    if n <= 0:
        raise ValueError(f"{key} 必须为正整数，当前为 {n}")
    return n


def _resolve_path(raw: str | None, default: Path) -> Path:
    """没配就用代码常量；配了相对路径按项目根解析，绝对路径原样使用。"""
    if raw is None or not str(raw).strip():
        return Path(default)
    p = Path(str(raw).strip())
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def _build(root: Path, dimensions_db: Path,
           num_tables: Mapping[str, int]) -> ReadConfig:
    profiles = {}
    for name, spec in AVL_DB_SPECS.items():
        cols = TABLE_SCHEMAS[spec.schema]
        profiles[name] = DbProfile(
            name=name,
            project=spec.project,
            db_file=spec.db_file,
            db_root=root,
            columns=tuple(c[0] for c in cols),
            schema=tuple(cols),
            primary_key=tuple(TABLE_PRIMARY_KEYS[spec.schema]),
            where=spec.where,
            order_by=spec.order_by,
            sharded=spec.sharded,
            num_tables=num_tables.get(name, 0) if spec.sharded else 0,
        )
    return ReadConfig(avl_db_root=root, dimensions_db=dimensions_db,
                      profiles=profiles)


def _from_env(env_path: Path) -> ReadConfig:
    v = _read_env(env_path)
    root = _resolve_path(v.get("AVL_DB_ROOT"), DEFAULT_AVL_DB_ROOT)
    dimensions_db = _resolve_path(v.get("DIMENSIONS_DB"),
                                  DEFAULT_DIMENSIONS_DB)
    nts = dict(NUM_TABLES_DEFAULTS)
    for avl, key in NUM_TABLES_ENV_KEYS.items():
        if key in v:
            nts[avl] = _positive_int(v[key], key)
    return _build(root, dimensions_db, nts)


@lru_cache(maxsize=1)
def _defaults() -> ReadConfig:
    """完全无参调用时复用同一份配置快照，避免 reader / dimensions 各读一次 .env。"""
    return _from_env(ENV_PATH)


def load(*, avl_db_root=None, dimensions_db=None, num_tables=None,
         env_path=None) -> ReadConfig:
    """装配在线侧读取配置；返回冻结的 ``ReadConfig``。

    - 无参：读 ``services/.env``（不存在则用代码内默认值），结果进程内缓存一次；
    - ``avl_db_root`` / ``dimensions_db``：覆盖库文件位置（离线自检注入临时目录）；
    - ``num_tables``：``{AVL 名: 分表数}``，覆盖分表数；
    - ``env_path``：改用另一份 KEY=VALUE 配置文件。
    """
    if (env_path is None and avl_db_root is None and dimensions_db is None
            and num_tables is None):
        return _defaults()
    base = (_defaults() if env_path is None
            else _from_env(Path(env_path)))
    root = (_resolve_path(str(avl_db_root), base.avl_db_root)
            if avl_db_root is not None else base.avl_db_root)
    dim = (_resolve_path(str(dimensions_db), base.dimensions_db)
           if dimensions_db is not None else base.dimensions_db)
    nts = {name: p.num_tables for name, p in base.profiles.items()
           if p.sharded}
    if num_tables:
        nts.update({k: _positive_int(str(v), f"{k}_NUM_TABLES")
                    for k, v in num_tables.items()})
    return _build(root, dim, nts)


def default_dimensions_db() -> Path:
    """``dimensions.py`` 的默认库路径（与 ``load()`` 同一份配置快照）。"""
    return load().dimensions_db


def validate(config) -> None:
    """在线侧自己的契约校验：列 / 查询键 / 排序 / 分表声明逐一核对。

    只检查本模块声明，不 import 写入侧任何东西；配置漂移在装配时暴露。
    """
    profiles = config.profiles if isinstance(config, ReadConfig) else config
    if set(profiles) != set(AVL_DB_SPECS):
        raise ValueError(
            "读取配置的 AVL 集合与 AVL_DB_SPECS 不一致；"
            f"spec={sorted(AVL_DB_SPECS)} config={sorted(profiles)}")
    for name, p in profiles.items():
        cols = set(p.columns)
        for col in p.where:
            if col not in cols:
                raise ValueError(
                    f"{name}: 查询键 {col} 不在表结构列中")
        if p.order_by not in (None, "op_time DESC"):
            raise ValueError(f"{name}: 排序只接受静态声明，不接受 {p.order_by!r}")
        if p.sharded:
            if "product_no" not in cols:
                raise ValueError(f"{name}: 分表 AVL 的表结构必须含 product_no")
            if "product_no" not in p.where:
                raise ValueError(f"{name}: 分表 AVL 的查询键必须含 product_no")
            if p.num_tables <= 0:
                raise ValueError(f"{name}: 分表 AVL 需要 NUM_TABLES")
        elif p.num_tables != 0:
            raise ValueError(f"{name}: 固定表不应配置 NUM_TABLES")


__all__ = [
    "ENV_PATH", "DEFAULT_AVL_DB_ROOT", "DEFAULT_DIMENSIONS_DB",
    "TABLE_SCHEMAS", "TABLE_PRIMARY_KEYS", "AVL_DB_SPECS", "AvlDbSpec",
    "SELLING_POINTS_TABLE", "SELLING_POINTS_PRIMARY_KEY",
    "SELLING_POINTS_REQUIRED_COLUMNS", "SELLING_POINTS_OPTIONAL_COLUMNS",
    "SELLING_POINTS_TAG_COLUMNS", "SELLING_POINTS_SELECT_COLUMNS",
    "SELLING_POINTS_SCHEMA", "shard_index", "table_name",
    "DbProfile", "ReadConfig", "load", "default_dimensions_db", "validate",
]
