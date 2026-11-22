# -*- coding: utf-8 -*-
"""控损 AVL 解析（逻辑 5 列，``strategy_name`` 含裸换行、需续行重组）。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from ..avl_specs import PRODUCT_NO_INDEX
from ..protocols import ParseStats, Row

# 以行首 YYYY-MM-DD 判定新记录起始
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def strip_eol(line: str) -> str:
    """剥行尾：只去 ``\\n``，再只剥**一个** ASCII 反斜杠。"""
    content = line.rstrip("\n")
    # 只剥一个 ASCII 反斜杠：rstrip('\\') 会误删 GBK 尾字节为 0x5C 的汉字。
    if content.endswith("\\"):
        content = content[:-1]
    return content


def split_head(content: str) -> list[str] | None:
    """把日期起始行切成 5 列。"""
    parts = content.split("\t")
    if len(parts) >= 5:
        return [parts[0], parts[1], parts[2], parts[3], parts[4]]
    if len(parts) == 4:
        return [parts[0], parts[1], parts[2], parts[3], ""]
    return None


class ContinuationParser:
    """按日期行重组续行，产出 5 列逻辑记录。"""

    def __init__(self):
        self.stats = ParseStats()

    def parse(self, path: Path) -> Iterator[Row]:
        """流式解析本地 AVL，产出 5 列元组生成器（不碰 DB / 网络 / 日志）。"""
        self.stats.reset()
        self.stats.encoding = "utf-8"

        buffer = None

        with open(Path(path), "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                content = strip_eol(line)
                if not content:
                    continue
                if DATE_RE.match(content):
                    if buffer is not None:
                        if str(buffer[PRODUCT_NO_INDEX]).strip():
                            self.stats.rows += 1
                            yield tuple(buffer)
                        else:
                            self.stats.skipped += 1
                    buffer = split_head(content)
                    if buffer is None:
                        self.stats.malformed_heads += 1
                elif buffer is not None:
                    buffer[4] += "\n" + content
                else:
                    self.stats.orphan_continuations += 1

            if buffer is not None:
                if str(buffer[PRODUCT_NO_INDEX]).strip():
                    self.stats.rows += 1
                    yield tuple(buffer)
                else:
                    self.stats.skipped += 1


__all__ = ["ContinuationParser", "DATE_RE", "strip_eol", "split_head"]
