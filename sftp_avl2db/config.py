# -*- coding: utf-8 -*-
"""配置：唯一来源 ``sftp_avl2db/.env``，唯一入口 ``load_profiles()``。

``ENV_PATH`` / ``RUNTIME_DIR`` 都按 ``__file__`` 定位；不读系统环境变量，
``import`` 零 I/O。配置分组与键说明见 ``README.md``。

一个 AVL 一份 profile：``name`` 既是文件前缀、db 文件名，也是 CLI ``--task``
值；``project`` 决定 ``files/`` 与 ``sqlite_db/`` 下的业务文件夹，同一业务的
多个 AVL 共用这一对文件夹，靠文件名体现归属。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ENV_PATH = Path(__file__).with_name(".env")
RUNTIME_DIR = Path(__file__).with_name("runtime")

# AVL 声明表：(AVL 名 / 文件前缀, 项目名)。
# 项目名同时用于：.env 键前缀（大写）、files/ 与 sqlite_db/ 的目录名。
# 每个 AVL 的其他静态信息（schema / parser / 是否分表 / 查询声明）都在
# avl_specs.py；表名自动取 AVL 名，产品 ID 归 main.py，SQLite 连接配置
# 在 sink/sqlite_sink.py 全局统一，本层都不声明。
# 新增 AVL = 这里加一行 + avl_specs.py 加一份声明。
_AVLS = (
    ("F09554", "opportunity_recall"),
    ("F09555", "opportunity_recall"),
    ("F09556", "opportunity_recall"),
    ("F09591", "loss_control"),
    # 诊断新线：8 张维表 / 对应关系表，各一个 db，统一落在 diagnosis/ 目录。
    ("F09627", "diagnosis"),
    ("F09628", "diagnosis"),
    ("F09629", "diagnosis"),
    ("F09630", "diagnosis"),
    ("F09631", "diagnosis"),
    ("F09632", "diagnosis"),
    ("F09633", "diagnosis"),
    ("F09635", "diagnosis"),
)


# ---------------------------------------------------------------------------
# 读 .env：只认整行 # 注释与 KEY=VALUE
# ---------------------------------------------------------------------------
def _read_env(env_path) -> dict[str, str]:
    """读配置文件；文件不存在直接报错，不用脏值继续跑。"""
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


def _int(v: dict[str, str], key: str) -> int:
    raw = _str(v, key)
    try:
        n = int(raw)
    except ValueError as e:
        raise ValueError(
            f"{key} 必须是正整数，当前为 {raw!r}（{ENV_PATH}）") from e
    if n <= 0:
        raise ValueError(f"{key} 必须为正整数，当前为 {n}（{ENV_PATH}）")
    return n


# ---------------------------------------------------------------------------
# 冻结配置对象（字段无默认值：一律由 load_profiles() 装配）
# ---------------------------------------------------------------------------
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


@dataclass(frozen=True)
class TaskProfileConfig:
    """一个 AVL 入库任务的完整声明（不可变）。

    四个路径都从 ``RUNTIME_DIR``（或显式传入的 ``runtime_dir``）派生，与 CWD
    无关；本地 AVL 与 db 输出按 ``project`` 归档到同一业务文件夹。
    """

    name: str                                # AVL 名 / 文件前缀 / {name}.db
    project: str                             # 项目名：输入输出文件夹
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
    parser: str                              # "flat" | "continuation" | "dimension"
    sharded: bool                            # 该 db 内是否按 product_no 分表
    num_tables: int                          # 分表数；固定表为 0
    remote_name_tmpl: str

    @property
    def columns(self) -> list[str]:
        """列名顺序（写入侧 DDL 与读取侧 dict 组装共用）。"""
        return [c[0] for c in self.schema]

    @property
    def fetched_path(self) -> Path:
        return self.state_dir / f"{self.name}.fetched"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / f"{self.name}.lock"


# ---------------------------------------------------------------------------
# 唯一入口
# ---------------------------------------------------------------------------
def load_profiles(runtime_dir=None) -> dict[str, TaskProfileConfig]:
    """读包内 ``.env``、按 AVL 装配、跑契约校验，返回冻结的 profile 表。

    ``runtime_dir=None`` → 包内 ``runtime/``（生产 / 在线侧都用这个）。
    显式传目录 → 产出隔离到该目录；相对路径按 CWD 解析，部署建议用绝对路径
    或把 ``runtime/`` 做成软链。

    ``avl_specs`` / ``table_schemas`` 在函数内延迟导入，保持模块加载时零依赖；
    ``validate_specs()`` 在返回前拦住表结构与声明的漂移。
    """
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

    profiles = {}
    for name, project in _AVLS:
        spec = spec_of(name)
        if spec.project != project:
            raise ValueError(
                f"{name}: config 项目名 {project} 与 avl_specs 的 {spec.project} 不一致")
        key = project.upper()
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
            num_tables=_int(v, f"{key}_NUM_TABLES") if spec.sharded else 0,
            remote_name_tmpl="{prefix}{date}000000.AVL",
        )

    validate_specs(profiles)
    return profiles


def remote_name_for(profile: TaskProfileConfig, prefix: str,
                    date_str: str) -> str:
    """拼一个远程文件名；编排器与 ``iter_remote_names()`` 共用这一处定义。"""
    return profile.remote_name_tmpl.format(prefix=prefix, date=date_str)


def iter_remote_names(profile: TaskProfileConfig, date_str: str) -> Iterable[str]:
    """按 ``profile.file_prefixes`` 的顺序拼出该日期的全部远程文件名。"""
    for prefix in profile.file_prefixes:
        yield remote_name_for(profile, prefix, date_str)
