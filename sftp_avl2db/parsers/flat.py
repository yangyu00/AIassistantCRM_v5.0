"""Flat AVL 解析（制表符分隔、一行一条、无续行）。"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..protocols import ParseStats, Row

ENCODING = "utf-8"


class FlatColumnParser:
    """一行一条记录的定长列解析，所有列原样保留文本。"""

    def __init__(self, column_count: int, primary_key_indexes=()):
        if column_count <= 0:
            raise ValueError(f"column_count 必须为正整数，当前为 {column_count}")
        self.column_count = int(column_count)
        self.primary_key_indexes = tuple(int(i) for i in primary_key_indexes)
        for i in self.primary_key_indexes:
            if not 0 <= i < self.column_count:
                raise ValueError(
                    f"主键列下标 {i} 越界（共 {self.column_count} 列）")
        self.stats = ParseStats()

    def parse(self, path: Path) -> Iterator[Row]:
        """流式解析本地 AVL，产出 ``column_count`` 列的元组生成器。"""
        self.stats.reset()
        self.stats.encoding = ENCODING

        with open(Path(path), "r", encoding=ENCODING, errors="ignore") as f:
            for line in f:
                # 只去行尾换行、去 BOM：不剥首尾制表符，否则「业务类型为空」
                # 「末列为空」这类合法空列会被整行 strip 吃掉。
                content = line.rstrip("\r\n").lstrip("\ufeff")
                if not content.strip():
                    continue
                fields = content.split("\t")
                if len(fields) < self.column_count:
                    fields.extend([""] * (self.column_count - len(fields)))
                elif len(fields) > self.column_count:
                    fields = fields[:self.column_count]

                if any(not str(fields[i]).strip()
                       for i in self.primary_key_indexes):
                    self.stats.skipped += 1
                    continue
                self.stats.rows += 1
                yield tuple(fields)


__all__ = ["FlatColumnParser", "ENCODING"]
