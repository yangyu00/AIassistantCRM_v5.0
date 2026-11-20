# -*- coding: utf-8 -*-
"""配置：唯一来源 ``sftp_avl2db/.env``，唯一入口 ``load_profiles()``。

``ENV_PATH`` / ``RUNTIME_DIR`` 都按 ``__file__`` 定位；不读系统环境变量，
``import`` 零 I/O。配置分组与键说明见 ``Readme.md``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ENV_PATH = Path(__file__).with_name(".env")
RUNTIME_DIR = Path(__file__).with_name("runtime")

# 任务声明表：(任务名, .env 键前缀, 解析器, 是否分表)。
# schema / mode / table_name 不在这里重复：都取自 dbspec（唯一真源），
# 新增任务在这里加一行 + 写进 .env。
_TASKS = (
    ("opportunity_recall", "OPPORTUNITY_RECALL", "flat", False),
    ("loss_control", "LOSS_CONTROL", "continuation", True),
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


def _list(v: dict[str, str], key: str) -> list[str]:
    return [x.strip() for x in _str(v, key).split(",") if x.strip()]


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
    """一个 SFTP AVL 入库任务的完整声明（不可变）。

    字段分四组：任务身份、连接、策略、路径与表结构。四个路径都从
    ``RUNTIME_DIR``（或显式传入的 ``runtime_dir``）派生，与 CWD 无关。
    """

    name: str
    file_prefixes: tuple[str, ...]
    sftp: SftpConfig
    retention: RetentionConfig
    local_dir: Path
    db_dir: Path
    state_dir: Path
    log_dir: Path
    schema: tuple[tuple[str, str], ...]
    parser: str                              # "flat" | "continuation"
    mode: str                                # 同 dbspec.QUERY_SPECS[name]["mode"]
    num_tables: int
    table_name_prefix: str
    table_name: str                          # 单库单表专用，由 QUERY_SPECS 派生
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
    """读包内 ``.env``、装配两个任务、跑契约校验，返回冻结的 profile 表。

    ``runtime_dir=None`` → 包内 ``runtime/``（生产 / 在线侧都用这个）。
    显式传目录 → 产出隔离到该目录；相对路径按 CWD 解析，部署建议用绝对路径
    或把 ``runtime/`` 做成软链。

    ``dbspec`` 在函数内延迟导入，保持模块加载时零依赖；``validate_specs()``
    在返回前拦住表结构与查询契约的漂移。
    """
    from .dbspec import DB_SCHEMAS, query_spec, validate_specs

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
    for name, prefix, parser, sharded in _TASKS:
        spec = query_spec(name)
        profiles[name] = TaskProfileConfig(
            name=name,
            file_prefixes=tuple(_list(v, f"{prefix}_FILE_PREFIXES")),
            sftp=sftp,
            retention=RetentionConfig(
                batch_size=_int(v, f"{prefix}_BATCH_SIZE"),
                data_days=_int(v, f"{prefix}_DATA_EXPIRE_DAYS"),
                local_file_days=_int(v, f"{prefix}_LOCAL_FILE_EXPIRE_DAYS"),
                poll_seconds=_int(v, f"{prefix}_POLL_INTERVAL_SECONDS"),
            ),
            local_dir=root / "files" / name,
            db_dir=root / "sqlite_db" / name,
            state_dir=root / "state",
            log_dir=root / "log",
            schema=tuple(tuple(c) for c in DB_SCHEMAS[name]),
            parser=parser,
            mode=spec["mode"],
            num_tables=_int(v, f"{prefix}_NUM_TABLES") if sharded else 0,
            table_name_prefix=(_str(v, f"{prefix}_TABLE_NAME_PREFIX")
                               if sharded else ""),
            table_name=spec.get("table_name", ""),
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
