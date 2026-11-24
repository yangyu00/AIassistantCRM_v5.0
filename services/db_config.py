"""在线业务侧**独立维护**的各 db 文件读取配置与静态声明。"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Mapping

MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parent
ENV_PATH = MODULE_DIR / ".env"

# 写入侧把业务库产出到 sftp_avl2db/runtime/sqlite_db/{project}/{avl}.db；
# 在线侧只是「按自己声明的路径去读同一份文件」，不引用写入侧的配置代码。
DEFAULT_AVL_DB_ROOT: Final = (PROJECT_ROOT / "sftp_avl2db" / "runtime"
                              / "sqlite_db")
DEFAULT_DIMENSIONS_DB: Final = (PROJECT_ROOT / "local_excel2db" / "runtime"
                                / "sqlite_db" / "dimensions.db")

NUM_TABLES_ENV_KEYS: Final = {
    "F09591": "LOSS_CONTROL_NUM_TABLES",   # 控损
    "F09633": "F09633_NUM_TABLES",         # 诊断·用户场景对应关系
}
NUM_TABLES_DEFAULTS: Final = {"F09591": 128, "F09633": 128}

# 业务表的时间列（排序 / 新鲜度过滤都只认这一个静态列名，不接受外部字符串）。
TIME_COLUMN: Final = "op_time"
# 写入侧在每个 {AVL}.db 内维护的批次元数据表；在线侧按同名只读。
# 两侧各自声明，磁盘上的 db 文件是唯一交集。
AVL_META_TABLE: Final = "_avl_meta"
# 新鲜度窗口：只召回 op_time 落在 [最新批次-(N-1)天, 最新批次] 的数据。
# N=0 关闭过滤；N=1 仅取最新一批。
DEFAULT_FRESHNESS_WINDOW_DAYS: Final = 2
FRESHNESS_WINDOW_ENV_KEY: Final = "AVL_FRESHNESS_WINDOW_DAYS"

# 一、各业务库的表字段（与写入侧建表 DDL 对齐；冗余声明便于在线侧独立理解）
#     key = schema 名；值 = ((列名, 类型), ...)，顺序即 SELECT * 的列顺序
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
    # 诊断 8 张表：一个 AVL 一个 db，列顺序 = AVL 制表符分隔顺序
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
    # 诊断按话术里的「#标签名&」反查 tag_id（= IOP 的 ITEMCODE），
    # 故 F09635 以标签名为主键，与写入侧声明保持一致。
    "tagid_tagname": ("tag_name",),
}


# 二、每个 AVL 对应一个 db 文件 + 一份在线查询声明
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
    # F09635 按「标签名」查 tag_id（= IOP 的 ITEMCODE），主键 / 查询键均为 tag_name
    "F09635": AvlDbSpec("F09635", "diagnosis", "diagnosis/F09635.db",
                        "tagid_tagname", ("tag_name",), "op_time DESC"),
}


# 三、卖点维表 dimensions.db（在线侧自己声明，不读写入侧 registry.json）
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

# 「标签转换维表.xlsx」写出的同名表：把 IOP 的 ITEMNAME 换成营销助手显示名字。
# 写入侧在 registry.json 里声明 label_id / display_name / platform_label_name；
# 在线侧只读前两列，列名在这里独立声明，不引用写入侧配置。
LABEL_NAME_MAPPING_TABLE: Final = "label_name_mapping"
LABEL_NAME_MAPPING_COLUMNS: Final = ("label_id", "display_name")


# 四、分表口径与表名（物理布局；写入侧独立实现了同样的规则）
def shard_index(product_no, num_tables: int) -> int:
    """分表号：``zlib.crc32(str(product_no).encode("utf-8")) % NUM_TABLES``。"""
    p_no = product_no if product_no else "NULL"
    return zlib.crc32(str(p_no).encode("utf-8")) % num_tables


def table_name(base: str, shard: int | None = None) -> str:
    """库内表名：固定表 = AVL 名，分表 = ``{AVL 名}_{分表号}``。"""
    return base if shard is None else f"{base}_{shard}"


# 五、装配：把静态声明 + .env / 显式参数变成一份只读配置
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
    # 0 = 关闭新鲜度过滤；N >= 1 = 只召回最近 N 个 op_time 日期的批次。
    freshness_window_days: int = DEFAULT_FRESHNESS_WINDOW_DAYS


def _read_env(path) -> dict[str, str]:
    """读 KEY=VALUE。"""
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


def _nonnegative_int(raw: str, key: str) -> int:
    try:
        n = int(raw)
    except ValueError as e:
        raise ValueError(f"{key} 必须是非负整数，当前为 {raw!r}") from e
    if n < 0:
        raise ValueError(f"{key} 必须是非负整数，当前为 {n}")
    return n


def _resolve_path(raw: str | None, default: Path) -> Path:
    """没配就用代码常量。"""
    if raw is None or not str(raw).strip():
        return Path(default)
    p = Path(str(raw).strip())
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def _build(root: Path, dimensions_db: Path,
           num_tables: Mapping[str, int],
           freshness_window_days: int = DEFAULT_FRESHNESS_WINDOW_DAYS) -> ReadConfig:
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
                      profiles=profiles,
                      freshness_window_days=freshness_window_days)


def _from_env(env_path: Path) -> ReadConfig:
    v = _read_env(env_path)
    root = _resolve_path(v.get("AVL_DB_ROOT"), DEFAULT_AVL_DB_ROOT)
    dimensions_db = _resolve_path(v.get("DIMENSIONS_DB"),
                                  DEFAULT_DIMENSIONS_DB)
    nts = dict(NUM_TABLES_DEFAULTS)
    for avl, key in NUM_TABLES_ENV_KEYS.items():
        if key in v:
            nts[avl] = _positive_int(v[key], key)
    window = (DEFAULT_FRESHNESS_WINDOW_DAYS
              if FRESHNESS_WINDOW_ENV_KEY not in v
              else _nonnegative_int(v[FRESHNESS_WINDOW_ENV_KEY],
                                    FRESHNESS_WINDOW_ENV_KEY))
    return _build(root, dimensions_db, nts, window)


@lru_cache(maxsize=1)
def _defaults() -> ReadConfig:
    """完全无参调用时复用同一份配置快照，避免 reader / dimensions 各读一次 .env。"""
    return _from_env(ENV_PATH)


def load(*, avl_db_root=None, dimensions_db=None, num_tables=None,
         env_path=None, freshness_window_days=None) -> ReadConfig:
    """装配在线侧读取配置。"""
    if (env_path is None and avl_db_root is None and dimensions_db is None
            and num_tables is None and freshness_window_days is None):
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
    window = (base.freshness_window_days if freshness_window_days is None
              else _nonnegative_int(str(freshness_window_days),
                                    FRESHNESS_WINDOW_ENV_KEY))
    return _build(root, dim, nts, window)


def default_dimensions_db() -> Path:
    """``dimensions.py`` 的默认库路径（与 ``load()`` 同一份配置快照）。"""
    return load().dimensions_db


def validate(config) -> None:
    """在线侧自己的契约校验：列 / 查询键 / 排序 / 分表声明逐一核对。"""
    profiles = config.profiles if isinstance(config, ReadConfig) else config
    if (isinstance(config, ReadConfig)
            and config.freshness_window_days < 0):
        raise ValueError(
            f"freshness_window_days 不能为负：{config.freshness_window_days}")
    if set(profiles) != set(AVL_DB_SPECS):
        raise ValueError(
            "读取配置的 AVL 集合与 AVL_DB_SPECS 不一致；"
            f"spec={sorted(AVL_DB_SPECS)} config={sorted(profiles)}")
    for name, p in profiles.items():
        cols = set(p.columns)
        if TIME_COLUMN not in cols:
            raise ValueError(f"{name}: 表结构缺少 {TIME_COLUMN} 列")
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
    "TIME_COLUMN", "AVL_META_TABLE", "DEFAULT_FRESHNESS_WINDOW_DAYS",
    "FRESHNESS_WINDOW_ENV_KEY",
    "TABLE_SCHEMAS", "TABLE_PRIMARY_KEYS", "AVL_DB_SPECS", "AvlDbSpec",
    "SELLING_POINTS_TABLE", "SELLING_POINTS_PRIMARY_KEY",
    "SELLING_POINTS_REQUIRED_COLUMNS", "SELLING_POINTS_OPTIONAL_COLUMNS",
    "SELLING_POINTS_TAG_COLUMNS", "SELLING_POINTS_SELECT_COLUMNS",
    "SELLING_POINTS_SCHEMA",
    "LABEL_NAME_MAPPING_TABLE", "LABEL_NAME_MAPPING_COLUMNS",
    "shard_index", "table_name",
    "DbProfile", "ReadConfig", "load", "default_dimensions_db", "validate",
]
