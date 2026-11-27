"""配置：唯一来源 ``sftp_avl2db/.env``，唯一入口 ``load_profiles()``。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ENV_PATH = Path(__file__).with_name(".env")
RUNTIME_DIR = Path(__file__).with_name("runtime")

# AVL 声明表；项目名仅用于 files/ 与 sqlite_db/ 目录分组，运行参数按 <AVL>_* 声明。
_AVLS = (
    ("F09554", "opportunity_recall"),
    ("F09555", "opportunity_recall"),
    ("F09556", "opportunity_recall"),
    ("F09591", "loss_control"),
    # 诊断：8 张维表 / 对应关系表，各一个 db，统一落在 diagnosis/ 目录。
    ("F09627", "diagnosis"),
    ("F09628", "diagnosis"),
    ("F09629", "diagnosis"),
    ("F09630", "diagnosis"),
    ("F09631", "diagnosis"),
    ("F09632", "diagnosis"),
    ("F09633", "diagnosis"),
    ("F09635", "diagnosis"),
)


# 读 .env：只认整行 # 注释与 KEY=VALUE
def _read_env(env_path) -> dict[str, str]:
    """读配置文件。"""
    path = Path(env_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"未找到配置文件 {path}；请从 .env.example 复制一份并填写")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key:
            values[key] = value
    return values


def _str(v: dict[str, str], key: str) -> str:
    value = v.get(key, "").strip()
    if not value:
        raise RuntimeError(f"缺少配置项 {key}（{ENV_PATH}）")
    return value


def _int(v: dict[str, str], key: str, *, min_value: int = 1) -> int:
    """读一个整数键。"""
    raw = _str(v, key)
    label = "正整数" if min_value > 0 else "非负整数"
    try:
        n = int(raw)
    except ValueError as e:
        raise ValueError(
            f"{key} 必须是{label}，当前为 {raw!r}（{ENV_PATH}）") from e
    if n < min_value:
        raise ValueError(f"{key} 必须是{label}，当前为 {n}（{ENV_PATH}）")
    return n


def _num_tables(v: dict[str, str], name: str) -> int:
    """分表数：只认 ``<AVL>_NUM_TABLES``，没有项目级键、没有兜底。"""
    return _int(v, f"{name}_NUM_TABLES")


def _flag(v: dict[str, str], key: str, *, default: int | None = None) -> int:
    """读一个 ``0`` 关闭 / ``1`` 开启的模式开关；只认这两个值。"""
    if default is not None and key not in v:
        return default
    n = _int(v, key, min_value=0)
    if n not in (0, 1):
        raise ValueError(
            f"{key} 只能是 0（关闭）或 1（开启），当前为 {n}（{ENV_PATH}）")
    return n


# 冻结配置对象（字段无默认值：一律由 load_profiles() 装配）
@dataclass(frozen=True)
class SftpConfig:
    host: str
    port: int
    username: str
    remote_path: str
    password: str = field(repr=False)        # repr 屏蔽凭据


@dataclass(frozen=True)
class RetentionConfig:
    batch_size: int
    data_days: int
    local_file_days: int
    poll_seconds: int
    # 库表过期清理的轮转周期（天）：0=每轮全量，1=每天全量一次，
    # N=每天只清 1/N 分表、N 天轮完。固定表不参与轮转，仍每天全清一次。
    # 每个 AVL 独立取值，无项目级兜底。
    purge_cycle_days: int
    # 全量替换模式：0=关闭（按主键 upsert 原地覆盖）；
    # 1=开启（写入最新 AVL 前 DROP 掉本 AVL 全部目标表并按同一 DDL 重建）。
    # 全局键 AVL_DROP_BEFORE_INSERT 打底，可用 <AVL>_DROP_BEFORE_INSERT 覆盖。
    drop_before_insert: int


@dataclass(frozen=True)
class TaskProfileConfig:
    """一个 AVL 入库任务的完整声明（不可变）。"""

    name: str                                # AVL 名 / 文件前缀 / {name}.db / .env 键前缀
    project: str                             # 项目名：仅用于输入输出文件夹分组
    file_prefixes: tuple[str, ...]           # 恒为 (name,)
    sftp: SftpConfig
    retention: RetentionConfig
    local_dir: Path
    db_dir: Path
    state_dir: Path
    log_dir: Path
    schema: tuple[tuple[str, str], ...]
    schema_name: str                         # table_schemas.py 里的 key
    primary_key: tuple[str, ...]             # TABLE_PRIMARY_KEYS 注册的主键列（可复合）
    parser: str                              # "flat" | "continuation"
    sharded: bool                            # 该 db 内是否按 product_no 分表
    num_tables: int                          # 分表数；固定表为 0
    remote_name_tmpl: str

    @property
    def columns(self) -> list[str]:
        """列名顺序（写入侧建表 DDL 与解析器列数校验用）。"""
        return [c[0] for c in self.schema]

    @property
    def fetched_path(self) -> Path:
        return self.state_dir / f"{self.name}.fetched"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / f"{self.name}.lock"


# 唯一入口
def load_profiles(runtime_dir=None) -> dict[str, TaskProfileConfig]:
    """读包内 ``.env``、按 AVL 装配、跑契约校验，返回冻结的 profile 表。"""
    from .avl_specs import spec_of, validate_specs
    from .table_schemas import TABLE_PRIMARY_KEYS, TABLE_SCHEMAS

    v = _read_env(ENV_PATH)
    root = Path(runtime_dir) if runtime_dir is not None else RUNTIME_DIR

    sftp = SftpConfig(
        host=_str(v, "SFTP_HOST"),
        port=_int(v, "SFTP_PORT"),
        username=_str(v, "SFTP_USERNAME"),
        remote_path=_str(v, "SFTP_REMOTE_PATH"),
        password=_str(v, "SFTP_PASSWORD"),
    )

    # 全量替换模式：全局键打底，<AVL>_DROP_BEFORE_INSERT 可选覆盖。
    global_drop = _flag(v, "AVL_DROP_BEFORE_INSERT")

    profiles = {}
    for name, project in _AVLS:
        spec = spec_of(name)
        if spec.project != project:
            raise ValueError(
                f"{name}: config 项目名 {project} 与 avl_specs 的 {spec.project} 不一致")
        # key 恒为 AVL 名：轮询 / 清理 / 批次 / 分表数都按 db 粒度声明。
        key = name
        profiles[name] = TaskProfileConfig(
            name=name,
            project=project,
            file_prefixes=(name,),
            sftp=sftp,
            retention=RetentionConfig(
                batch_size=_int(v, f"{key}_BATCH_SIZE"),
                data_days=_int(v, f"{key}_DATA_EXPIRE_DAYS"),
                local_file_days=_int(v, f"{key}_LOCAL_FILE_EXPIRE_DAYS"),
                poll_seconds=_int(v, f"{key}_POLL_INTERVAL_SECONDS"),
                # 允许 0（每轮全量），但仍是必需键，不是缺省回落值。
                purge_cycle_days=_int(v, f"{key}_PURGE_CYCLE_DAYS",
                                      min_value=0),
                drop_before_insert=_flag(
                    v, f"{key}_DROP_BEFORE_INSERT", default=global_drop),
            ),
            local_dir=root / "files" / project,
            db_dir=root / "sqlite_db" / project,
            state_dir=root / "state",
            log_dir=root / "log",
            schema=tuple(TABLE_SCHEMAS[spec.schema]),
            schema_name=spec.schema,
            primary_key=tuple(TABLE_PRIMARY_KEYS[spec.schema]),
            parser=spec.parser,
            sharded=spec.sharded,
            num_tables=_num_tables(v, name) if spec.sharded else 0,
            remote_name_tmpl="{prefix}{date}000000.AVL",
        )

    validate_specs(profiles)
    return profiles


def remote_name_for(profile: TaskProfileConfig, prefix: str,
                    date_str: str) -> str:
    """拼一个远程文件名。"""
    return profile.remote_name_tmpl.format(prefix=prefix, date=date_str)


def iter_remote_names(profile: TaskProfileConfig, date_str: str) -> Iterable[str]:
    """按 ``profile.file_prefixes`` 的顺序拼出该日期的全部远程文件名。"""
    for prefix in profile.file_prefixes:
        yield remote_name_for(profile, prefix, date_str)
