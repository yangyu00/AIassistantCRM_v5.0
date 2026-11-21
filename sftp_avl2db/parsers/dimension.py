# -*- coding: utf-8 -*-
"""诊断新线维表 AVL 解析（制表符分隔、一行一条、无续行、纯文本）。

与商机回捞 ``FlatColumnParser`` 的区别：诊断表的末列多为话术 / 名称文本，
不能转 int；主键也不恒为第 3 列 ``product_no``，而是各表注册的自然键
（可复合）。因此这里只做「按制表符切分 → 补齐 / 截断到声明列数 → 空主键跳过」，
其余一列不动地交给写入层，由 SQLite 列类型做亲和转换。

AVL 固定 UTF-8；文件分隔符 ``\\t``、编码 UTF-8 见「话术涉及表结构.xls」。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..protocols import ParseStats, Row

ENCODING = "utf-8"


class DimensionFlatParser:
    """通用维表定长列解析。

    :param column_count: 目标列数（取自 ``profile.columns``）。
    :param primary_key_indexes: 主键列下标（由 ``build_parser()`` 按
        ``profile.primary_key`` 换算）；任一为空串则整条跳过并计入 ``skipped``。
    """

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
                # 只去行尾换行：不像 FlatColumnParser 那样 strip() 整行，
                # 否则首尾制表符会把「业务类型为空」「末列为空」吃掉。
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


__all__ = ["DimensionFlatParser", "ENCODING"]
