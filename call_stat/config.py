# -*- coding: utf-8 -*-
"""调用量统计配置：唯一来源 ``call_stat/.env``，唯一入口 ``load_config()``。

``ENV_PATH`` / ``RUNTIME_DIR`` 按 ``__file__`` 定位，与启动 CWD 无关；
产物路径一律由 ``RUNTIME_DIR`` + 纯文件名派生，配置里写 ``../`` 或绝对路径
会被拒绝并回落默认值，从代码上保证「输出产物不越出模块目录」。

``import call_stat`` 零 I/O、零第三方依赖；只有 ``load_config()`` 被调用时才读
``.env``，语法与 IOP / sftp_avl2db 一致（整行 ``#`` 注释 + ``KEY=VALUE``）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")
RUNTIME_DIR = Path(__file__).with_name("runtime")

_DEFAULTS = {
    "CALL_STAT_FLUSH_THRESHOLD": 100,
    "CALL_STAT_QUERY_PATH": "/api/callStat",
    "CALL_STAT_DB_FILENAME": "call_sum.db",
    "CALL_STAT_EXPORT_DIRNAME": "export",
}


def warn(message: str) -> None:
    """旁路功能统一告警口：只打印、不抛异常、不阻断在线服务。"""
    print(f"WARNING:: {message}", flush=True)


def _read_env(env_path) -> dict[str, str]:
    path = Path(env_path)
    if not path.is_file():
        warn(f"未找到 {path}，call_stat 全部回落内置默认值")
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key:
            values[key] = value
    return values


def _positive_int(raw, default: int) -> int:
    try:
        n = int(str(raw))
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _safe_component(raw, default: str, key: str) -> str:
    """产物只允许纯文件名；任何越界写法一律回落默认并告警。"""
    v = str(raw or "").strip()
    p = Path(v)
    if (not v or v in (".", "..") or "/" in v or "\\" in v
            or p.is_absolute() or p.name != v):
        warn(f"{key} 只允许包内 runtime/ 下的纯文件名，当前 {v!r} 非法，回落 {default!r}")
        return default
    return v


def _query_path(raw, default: str) -> str:
    v = str(raw or "").strip()
    if not v.startswith("/") or "?" in v or " " in v:
        warn(f"CALL_STAT_QUERY_PATH 非法 {v!r}，回落 {default!r}")
        return default
    return v


@dataclass(frozen=True)
class CallStatConfig:
    """不可变的运行配置；``db_path`` / ``export_dir`` 恒在 ``RUNTIME_DIR`` 内。"""

    flush_threshold: int
    query_path: str
    db_path: Path
    export_dir: Path


def load_config(*, env_path=None, runtime_dir=None) -> CallStatConfig:
    """读一次 ``.env`` 并冻结；缺文件 / 缺键 / 非法值一律回落默认值并告警。

    ``runtime_dir`` 仅供自检把产物隔离到 ``runtime/`` 的子目录，仍不允许越出
    ``RUNTIME_DIR``；生产不传参，配置来源与产物位置都锚在包目录内。
    """
    v = _read_env(Path(env_path) if env_path is not None else ENV_PATH)
    root = Path(runtime_dir).resolve() if runtime_dir is not None else RUNTIME_DIR.resolve()
    base = RUNTIME_DIR.resolve()
    if root != base and base not in root.parents:
        warn(f"runtime_dir={root} 越出 {base}，回落包内 runtime/")
        root = base
    d = _DEFAULTS

    def resolve(key, parser, *extra):
        # 缺键静默回落默认值；只有显式写了非法值才告警（与 IOP 的容错口径一致）
        return parser(v[key], d[key], *extra) if key in v else d[key]

    return CallStatConfig(
        flush_threshold=resolve("CALL_STAT_FLUSH_THRESHOLD", _positive_int),
        query_path=resolve("CALL_STAT_QUERY_PATH", _query_path),
        db_path=root / resolve("CALL_STAT_DB_FILENAME", _safe_component,
                               "CALL_STAT_DB_FILENAME"),
        export_dir=root / resolve("CALL_STAT_EXPORT_DIRNAME", _safe_component,
                                  "CALL_STAT_EXPORT_DIRNAME"),
    )
