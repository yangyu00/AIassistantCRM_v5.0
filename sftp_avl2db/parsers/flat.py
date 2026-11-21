# -*- coding: utf-8 -*-
"""Flat AVL 解析（制表符分隔、一行一条、无续行）。

商机回捞 ``F09554/55/56`` 与诊断新线 8 张维表共用本解析器：把一行按
``\\t`` 切成声明列数的元组，**所有列原样保留文本**，不做任何类型转换——
列类型交给 SQLite 的列亲和性处理（例如商机末列 ``city_id`` 声明为 INTEGER，
``"3"`` 会由 SQLite 存成整数 3，非数字文本则保留原样；该字段业务上不消费）。

差异只有「哪些列是主键」：由 ``TABLE_PRIMARY_KEYS`` 按 schema 注册，
``build_parser()`` 换算成下标传进来。商机主键是第 3 列 ``product_no``，
诊断维表是 ``scene`` / ``business_type, scene`` / ``product_id`` 等。
任一主键列为空则整条跳过并计入 ``ParseStats.skipped``。

AVL 固定 UTF-8；文件分隔符 ``\\t``、编码 UTF-8 见「话术涉及表结构.xls」。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..protocols import ParseStats, Row

ENCODING = "utf-8"


class FlatColumnParser:
    """一行一条记录的定长列解析，所有列原样保留文本。

    :param column_count: 目标列数（取自 ``profile.columns``）。
    :param primary_key_indexes: 主键列下标，由 ``build_parser()`` 按
        ``profile.primary_key`` 换算；任一为空串则整条跳过。留空表示不判空
        （仅用于单测直接构造，生产装配一律由 profile 传入）。
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
