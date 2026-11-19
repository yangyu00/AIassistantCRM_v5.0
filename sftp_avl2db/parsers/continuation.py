# -*- coding: utf-8 -*-
"""控损 AVL 解析（逻辑 5 列，``strategy_name`` 内含裸换行、需续行重组）。

``parse()`` 是生成器，产出 5 列元组交给 ``RecordSink``；不碰 DB / 网络 / 日志，
计数与诊断信号放在 ``stats`` 里供编排器按需记录。

以下行为刻意保真，不要顺手优化：

1. 以行首 ``YYYY-MM-DD`` 判定新逻辑记录起始，其余物理行是上一条第 5 列的续行，
   用 ``\\n`` 拼回；
2. ``_strip_eol`` 只 ``rstrip("\\n")``（**不剥 ``\\r``**），再只剥**一个** ASCII
   反斜杠；绝不能用 ``rstrip('\\\\')``，否则 GBK 尾字节为 ``0x5C`` 的汉字会被
   削掉一个字节变成乱码；
3. 起始行 ``>= 5`` 列时只取前 5 列（第 6 列起丢弃），``== 4`` 列补空
   ``strategy_name``，``< 4`` 列整条丢弃；
4. 空物理行（剥完反斜杠后为空）直接跳过，**不会**拼出多余的 ``\\n``；
5. 文件开头就出现续行、尚无 buffer → 丢弃；
6. 读完时最后一条记录单独产出；``product_no`` 为空的记录跳过并计入
   ``stats.skipped``。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List, Optional

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


def split_head(content: str) -> Optional[List[str]]:
    """把日期起始行切成 5 列；第 5 列允许为空。不可用时返回 None。"""
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
                        if str(buffer[2]).strip():
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
                if str(buffer[2]).strip():
                    self.stats.rows += 1
                    yield tuple(buffer)
                else:
                    self.stats.skipped += 1


__all__ = ["ContinuationParser", "DATE_RE", "strip_eol", "split_head"]
