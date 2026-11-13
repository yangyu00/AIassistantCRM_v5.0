# -*- coding: utf-8 -*-
"""tasks 独立配置：读取项目根目录 .env。

不依赖根目录 config.py / config.yaml，也不读取各 sftp_workdir_* 的 CONFIG。
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


def _sharded_task(prefix):
    conf = _task(prefix)
    conf["NUM_TABLES"] = _int(f"{prefix}_NUM_TABLES")
    conf["TABLE_NAME_PREFIX"] = _str(f"{prefix}_TABLE_NAME_PREFIX")
    return conf


TASKS = {
    "f09554_opp": _task("TASK1"),
    "f09591_loss": _sharded_task("TASK2"),
    "f09609_diag": _sharded_task("TASK3"),
}
