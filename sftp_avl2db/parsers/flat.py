# -*- coding: utf-8 -*-
"""回捞商机 AVL 解析（制表符分隔、一行一条、无续行）。

行为保真清单见 ``Readme.md``「刻意保真」一节；实现注释说明「为什么」。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..avl_specs import PRODUCT_NO_INDEX
from ..protocols import ParseStats, Row

ENCODING_CANDIDATES = ("utf-8", "gbk", "latin-1")


class FlatColumnParser:
    """一行一条记录的定长列解析。

    :param column_count: 目标列数（取自 ``profile.columns``，商机为 12）。
    """

    def __init__(self, column_count: int):
        if column_count <= 0:
            raise ValueError(f"column_count 必须为正整数，当前为 {column_count}")
        self.column_count = int(column_count)
        self.stats = ParseStats()

    def parse(self, path: Path) -> Iterator[Row]:
        """流式解析本地 AVL，产出 ``column_count`` 列的元组生成器。"""
        self.stats.reset()
        path = Path(path)

        # ---- 编码探测（结果记入 stats.encoding） ----
        fh = None
        for enc in ENCODING_CANDIDATES:
            try:
                fh = open(path, "r", encoding=enc, errors="ignore")
                self.stats.encoding = enc
                break
            except Exception:
                continue
        if fh is None:
            raise RuntimeError("无法识别文件编码")

        with fh as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                fields = line.split("\t")
                if len(fields) < self.column_count:
                    fields.extend([""] * (self.column_count - len(fields)))
                elif len(fields) > self.column_count:
                    fields = fields[:self.column_count]
                try:
                    val = fields[-1].strip()
                    fields[-1] = int(val) if val else 0
                except ValueError:
                    fields[-1] = 0
                if not str(fields[PRODUCT_NO_INDEX]).strip():
                    self.stats.skipped += 1
                    continue
                self.stats.rows += 1
                yield tuple(fields)


__all__ = ["FlatColumnParser", "ENCODING_CANDIDATES"]
