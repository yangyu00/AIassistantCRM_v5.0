# -*- coding: utf-8 -*-
"""``sftp_avl2db`` 的配置对象。

**import 本模块零 I/O**：不读 ``.env``、不连网络、不建目录、不写日志、不注册
全局 logger。配置只在显式调用 ``load_profiles()`` / ``load_values()`` 时加载，
其他项目也可直接用 ``TaskProfileConfig.from_mapping()`` 构造，完全不带 ``.env``。

dotenv 解析（``parse_dotenv`` / ``load_values``）从原 ``tasks/config.py`` 原样
平移，逐字未改：支持 ``#`` 注释、行内注释、``export``、单/双引号与 ``${VAR}``
插值，优先级「系统环境变量 > .env」，``TASKS_ENV_FILE`` 可指向其他路径。
唯一变化是从「模块级执行」改为「函数内执行」。

路径约定：默认运行时数据目录取仓库根下的 ``RUNTIME_DIRNAME``（沿用现有
``tasks/log`` / ``tasks/state`` / ``tasks/files`` / ``tasks/sqlite_db``）。
所有路径都是 ``TaskProfileConfig`` 的普通字段，由 ``from_mapping()`` 显式注入；
换根目录只需改默认工厂一处，写入侧与在线读取侧从同一份 profile 取值，
不需要任何迁移脚本。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

# 运行时数据目录名。沿用日志目录约定；因为路径全部由配置对象派生，改名不涉及
# 任何历史数据迁移逻辑，只是换一个默认值。
RUNTIME_DIRNAME = "tasks"

# ---- 原 tasks/config.py 的 dotenv 解析，逐字平移 ----
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


def default_project_root() -> Path:
    """包所在仓库 / 项目的根目录（惰性求值，模块级不调用）。"""
    return Path(__file__).resolve().parent.parent


def default_env_path(project_root: Path | None = None) -> Path:
    root = Path(project_root).resolve() if project_root else default_project_root()
    return root / ".env"


def load_values(env_path=None) -> dict:
    """读取配置值：系统环境变量 > .env；``TASKS_ENV_FILE`` 可覆盖 .env 路径。

    与原 ``tasks/config.py::load_values()`` 行为一致；缺 ``.env`` 且系统环境
    变量未提供 ``SFTP_HOST`` / ``SFTP_PASSWORD`` 时抛 ``FileNotFoundError``。
    本函数**只在被显式调用时**执行，import 不会触发。
    """
    path = Path(env_path or os.environ.get("TASKS_ENV_FILE") or default_env_path())
    values = dict(os.environ)
    if path.is_file():
        for k, v in parse_dotenv(path).items():
            values.setdefault(k, v)
    elif not all(k in values for k in ("SFTP_HOST", "SFTP_PASSWORD")):
        raise FileNotFoundError(f"未找到 .env：{path}；也可通过系统环境变量提供配置")
    return values


# ---- 配置对象 ----

@dataclass(frozen=True)
class SftpConfig:
    host: str
    port: int
    username: str
    password: str
    remote_path: str


@dataclass(frozen=True)
class RetentionConfig:
    batch_size: int = 5000
    data_days: int = 30
    local_file_days: int = 7
    poll_seconds: int = 600


@dataclass(frozen=True)
class TaskProfileConfig:
    """一个 SFTP AVL 入库任务的完整声明（不可变）。

    字段分四组：任务身份（name / file_prefixes）、连接（sftp）、策略
    （retention / parser / routing / num_tables / table_name_prefix）、
    路径（local_dir / db_dir / state_dir / log_dir）与表结构（schema）。
    远程文件名模板也做成字段，方便其他项目接不同命名规则。
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
    routing: str                             # "per_prefix" | "crc32_shards" | "fixed_table"
    num_tables: int = 0
    table_name_prefix: str = ""
    remote_name_tmpl: str = "{prefix}{date}000000.AVL"

    @property
    def columns(self) -> list[str]:
        """列名顺序（写入侧 DDL 与读取侧 dict 组装共用）。"""
        return [c[0] for c in self.schema]

    @property
    def fetched_path(self) -> Path:
        """断点续传清单，文件名沿用 ``{任务名}.fetched``。"""
        return self.state_dir / f"{self.name}.fetched"

    @property
    def lock_path(self) -> Path:
        """单实例锁，文件名沿用 ``{任务名}.lock``。"""
        return self.state_dir / f"{self.name}.lock"

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any]) -> "TaskProfileConfig":
        """从纯 dict 构造，**不读 .env、不依赖仓库内任何东西**（供其他项目 / 单测用）。

        必填：``name``、``file_prefixes``、``sftp``、``schema``、``parser``、``routing``。
        可选：``retention``（子 dict 或扁平键 ``batch_size`` 等）、
        ``project_root`` / ``runtime_dir`` / 四个路径字段、``num_tables``、
        ``table_name_prefix``、``remote_name_tmpl``。缺路径时按默认约定派生。
        """
        d = dict(d)
        name = str(d["name"])

        prefixes = tuple(str(x) for x in (d.get("file_prefixes") or ()))

        raw_sftp = d.get("sftp") or {}
        sftp = raw_sftp if isinstance(raw_sftp, SftpConfig) else SftpConfig(
            host=str(raw_sftp["host"]),
            port=int(raw_sftp["port"]),
            username=str(raw_sftp["username"]),
            password=str(raw_sftp["password"]),
            remote_path=str(raw_sftp["remote_path"]),
        )

        raw_ret = d.get("retention") or {}
        if isinstance(raw_ret, RetentionConfig):
            retention = raw_ret
        else:
            keys = ("batch_size", "data_days", "local_file_days", "poll_seconds")
            merged = {k: d[k] for k in keys if k in d}
            merged.update({k: raw_ret[k] for k in keys if k in raw_ret})
            retention = RetentionConfig(**merged)

        root = Path(d.get("project_root") or default_project_root()).resolve()
        runtime = (Path(d["runtime_dir"]).resolve() if d.get("runtime_dir")
                   else (root / RUNTIME_DIRNAME).resolve())

        def _path(key: str, default: str) -> Path:
            value = d.get(key)
            return Path(value).resolve() if value else (runtime / default).resolve()

        schema = tuple(tuple(c) for c in (d.get("schema") or ()))

        return cls(
            name=name,
            file_prefixes=prefixes,
            sftp=sftp,
            retention=retention,
            local_dir=_path("local_dir", f"files/{name}"),
            db_dir=_path("db_dir", f"sqlite_db/{name}"),
            state_dir=_path("state_dir", "state"),
            log_dir=_path("log_dir", "log"),
            schema=schema,
            parser=str(d["parser"]),
            routing=str(d["routing"]),
            num_tables=int(d.get("num_tables") or 0),
            table_name_prefix=str(d.get("table_name_prefix") or ""),
            remote_name_tmpl=str(d.get("remote_name_tmpl") or "{prefix}{date}000000.AVL"),
        )


# ---- CRM 项目默认装配（显式调用才执行）----

def _str(values: Mapping[str, str], key: str, env_path: Path) -> str:
    if not values.get(key):
        raise RuntimeError(f"缺少配置项 {key}（.env: {env_path}）")
    return values[key]


def _int(values: Mapping[str, str], key: str, env_path: Path) -> int:
    val = int(_str(values, key, env_path))
    if val <= 0:
        raise ValueError(f"{key} 必须为正整数，当前为 {val}")
    return val


def _list(values: Mapping[str, str], key: str, env_path: Path) -> list[str]:
    return [x.strip() for x in _str(values, key, env_path).split(",") if x.strip()]


def load_profiles(env_path=None, project_root=None) -> dict[str, TaskProfileConfig]:
    """构造 CRM 项目现成的两个任务配置，并在返回前做契约校验。

    等价于原 ``tasks/config.py`` 模块级 ``TASKS`` + ``_validate_specs()`` +
    ``db_dir()``，区别是**必须显式调用**：``import sftp_avl2db`` 不会读任何文件。

    ``crm_dbspec`` 只在函数内部延迟导入，因此把 ``sftp_avl2db`` 包单独 copy 到
    其他项目、用 ``from_mapping()`` 构造时，不需要携带该文件。
    """
    from crm_dbspec import DB_SCHEMAS, validate_specs

    root = Path(project_root).resolve() if project_root else default_project_root()
    path = Path(env_path or os.environ.get("TASKS_ENV_FILE") or default_env_path(root))
    v = load_values(path)

    sftp = {
        "host": _str(v, "SFTP_HOST", path),
        "port": _int(v, "SFTP_PORT", path),
        "username": _str(v, "SFTP_USERNAME", path),
        "password": _str(v, "SFTP_PASSWORD", path),
        "remote_path": _str(v, "SFTP_REMOTE_PATH", path),
    }

    def base(prefix: str) -> dict:
        return {
            "file_prefixes": _list(v, f"{prefix}_FILE_PREFIXES", path),
            "sftp": sftp,
            "retention": {
                "batch_size": _int(v, f"{prefix}_BATCH_SIZE", path),
                "data_days": _int(v, f"{prefix}_DATA_EXPIRE_DAYS", path),
                "local_file_days": _int(v, f"{prefix}_LOCAL_FILE_EXPIRE_DAYS", path),
                "poll_seconds": _int(v, f"{prefix}_POLL_INTERVAL_SECONDS", path),
            },
            "project_root": root,
        }

    profiles = {
        "opportunity_recall": TaskProfileConfig.from_mapping({
            **base("OPPORTUNITY_RECALL"),
            "name": "opportunity_recall",
            "schema": DB_SCHEMAS["opportunity_recall"],
            "parser": "flat",
            "routing": "per_prefix",
        }),
        "loss_control": TaskProfileConfig.from_mapping({
            **base("LOSS_CONTROL"),
            "name": "loss_control",
            "schema": DB_SCHEMAS["loss_control"],
            "parser": "continuation",
            "routing": "crc32_shards",
            "num_tables": _int(v, "LOSS_CONTROL_NUM_TABLES", path),
            "table_name_prefix": _str(v, "LOSS_CONTROL_TABLE_NAME_PREFIX", path),
        }),
    }

    validate_specs(profiles)
    return profiles


def iter_remote_names(profile: TaskProfileConfig, date_str: str) -> Iterable[str]:
    """按前缀与日期拼出远程文件名，顺序与现有 ``_remote_name`` 一致。"""
    for prefix in profile.file_prefixes:
        yield profile.remote_name_tmpl.format(prefix=prefix, date=date_str)
