# -*- coding: utf-8 -*-
"""IOP 自带的极简 ``.env`` 解析（无第三方依赖）。

规则与项目里 ``sftp_avl2db/config.py`` 的 dotenv 一致：``#`` 注释与行内注释、
``export`` 前缀、单/双引号、``${VAR}`` 插值。IOP 是可独立复用的包，**不跨包
import**，所以这里自带一份小实现，而不是依赖项目其它模块。

import 本模块零 I/O；只有 ``load()`` 会读文件，文件不存在时返回空 dict。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_QUOTED_RE = re.compile(r"""(?P<q>["'])(?P<v>.*?)(?<!\\)(?P=q)\s*(?:#.*)?$""", re.S)


def _unescape(value: str, quote: str) -> str:
    """双引号内还原常见转义；单引号只还原 ``\\'``。"""
    if quote == '"':
        return (value.replace(r"\n", "\n").replace(r"\t", "\t")
                .replace(r'\"', '"').replace(r"\\", "\\"))
    return value.replace(r"\'", "'")


def parse(text: str) -> dict[str, str]:
    """把 ``.env`` 文本解析成 dict。

    插值取值顺序：系统环境变量 > 本文件中前面已解析的键 > 保留原样。
    """
    parsed: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if not key:
            continue

        quoted = _QUOTED_RE.match(value)
        if quoted:
            value = _unescape(quoted.group("v"), quoted.group("q"))
        else:
            value = re.sub(r"\s+#.*$", "", value).rstrip()

        def expand(match) -> str:
            name = match.group(1) or match.group(2)
            return os.environ.get(name, parsed.get(name, match.group(0)))

        parsed[key] = _VAR_RE.sub(expand, value)
    return parsed


def load(path) -> dict[str, str]:
    """读取并解析 ``.env``；文件不存在时返回 ``{}``（配置回落内置默认值）。

    文件存在但读不了（权限 / 编码）会照常抛错，避免「改了却没生效」被静默吞掉。
    """
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    return parse(file_path.read_text(encoding="utf-8"))
