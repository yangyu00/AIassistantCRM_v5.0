"""项目根级运行配置：唯一来源 ``./.env``，唯一入口 ``load_config()``。

不读系统环境变量，没有第二套解析入口；
``main.py`` 与 ``start_project.sh`` 共用本模块，避免两处端口口径漂移。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")
DEFAULT_PORT = 8989
PORT_ENV_KEY = "CRM_PORT"


@dataclass(frozen=True)
class RootConfig:
    """不可变的根级运行配置。"""

    port: int


def _read_env(env_path) -> dict[str, str]:
    path = Path(env_path)
    if not path.is_file():
        # 缺文件不阻断：干净检出也能直接起服务，端口回落内置默认值。
        print(f"WARNING:: 未找到 {path}，{PORT_ENV_KEY} 回落内置默认值 {DEFAULT_PORT}",
              file=sys.stderr, flush=True)
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key:
            values[key] = value
    return values


def load_config(*, env_path=None) -> RootConfig:
    """读一次项目根 ``.env`` 并冻结；改完重启进程生效。"""
    v = _read_env(Path(env_path) if env_path is not None else ENV_PATH)
    if PORT_ENV_KEY not in v:
        # 缺键静默回落默认值。
        return RootConfig(port=DEFAULT_PORT)
    raw = v[PORT_ENV_KEY]
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"项目根 .env 的 {PORT_ENV_KEY} 必须是 1~65535 的整数，当前值: {raw!r}"
        ) from exc
    if not 1 <= port <= 65535:
        raise ValueError(
            f"项目根 .env 的 {PORT_ENV_KEY} 必须是 1~65535 的整数，当前值: {raw!r}"
        )
    # 端口写错会让服务起在意外端口上，显式非法值直接 fail-fast，不静默回落。
    return RootConfig(port=port)


def resolve_port(*, env_path=None) -> int:
    """给 ``start_project.sh`` 用的单行入口（只输出 port）。"""
    return load_config(env_path=env_path).port
