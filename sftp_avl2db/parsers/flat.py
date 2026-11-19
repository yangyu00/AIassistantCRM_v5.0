# -*- coding: utf-8 -*-
"""回捞商机 AVL 解析（制表符分隔、一行一条记录、无续行）。

本文件是 ``tasks/task_opportunity_recall.py::parse_and_save()`` 里**解析部分**
的逐行照搬，唯一变化是把「写入 SQLite」从循环里拿掉，改成 ``yield``：

    原：batch.append(tuple(fields)) → 攒够 BATCH_SIZE 后 executemany + commit
    新：yield tuple(fields)          → 由 RecordSink（阶段 3）负责攒批与提交

刻意保留的既有语义（**阶段 2 不做任何修正**，由 golden 基线与合成用例锁定）：

1. ``line.strip()`` 连行首行尾的制表符一起剥，尾随空列因此可能被吃掉；
2. 列数不足补空串、超出截断到 ``column_count``；
3. 最后一列按 int 转换，空串 / 非数字 → ``0``；
4. ``product_no``（第 3 列）为空则跳过，记入 ``stats.skipped``；
5. 编码探测循环仍是 ``("utf-8", "gbk", "latin-1")`` 且带 ``errors="ignore"``。
   注意这是**现状的保真复刻**：``errors="ignore"`` 会让 utf-8 打开几乎不抛异常，
   因此实际恒走 utf-8 分支，gbk / latin-1 不会命中。不要在这里「顺手修好」，
   否则 GBK 文件的产出会与现有生产结果不一致。

解析层不连网络、不建库、不写日志；计数放进 ``stats`` 供编排器按需记录。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

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

        # ---- 编码探测：逐字平移，只是把 log.info 换成 stats.encoding ----
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
                if not str(fields[2]).strip():
                    self.stats.skipped += 1
                    continue
                self.stats.rows += 1
                yield tuple(fields)


__all__ = ["FlatColumnParser", "ENCODING_CANDIDATES"]
