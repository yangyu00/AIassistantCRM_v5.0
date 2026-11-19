# -*- coding: utf-8 -*-
"""维表读取侧（在线业务用）：只读查库、按声明重建 dict、任何故障降级为 ``{}``。

- 默认仓储用 ``mode=ro`` 打开，**绝不创建/写入维表库**；每次查询用完即关连接。
- 不做进程级缓存：每次调用读一次全表，天然读到写入侧的最新值。
- ``_warned`` 为类级共享：同一类告警每进程只打印一次，避免刷屏；可注入
  ``warn_sink`` 收集告警做离线断言。
- ``registry`` 与 ``repository`` 均可注入，reader 自身不碰文件系统，便于单独测试
  或让宿主项目换数据源。

库的路径、表名、key/value 列都来自 ``Registry``，不硬编码。``lookup`` 支持两种形状：
  ``value``  单列  → ``{key: 单值}``
  ``values`` 多列  → ``{key: (值1, 值2, ...)}``，顺序即声明顺序
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import RegistryLoader
from .models import Registry
from .protocols import LogSink, ReadableRepository, Row
from .repository import SqliteDimensionRepository
from .transform import CutInScriptTransformer

_PRODUCT_TABLE = "product_selling_point_match"


def _default_warn_sink(msg: str) -> None:
    print(f"WARNING:: {msg}", flush=True)


class DimensionReader:
    """在线业务只读视图；构造时不建库、不读文件、不连库。"""

    # 类级而非实例级：请求内可能多次构造 reader，告警仍按进程去重。
    _warned: set[str] = set()

    def __init__(self, registry: Registry | None = None,
                 repository: ReadableRepository | None = None, *,
                 warn_sink: Callable[[str], None] | LogSink | None = None,
                 degraded_reason: str = "") -> None:
        self._registry = registry
        self._repository_arg = repository
        self._degraded_reason = degraded_reason
        self._warn_sink = warn_sink or _default_warn_sink

    @classmethod
    def from_registry_file(cls, path: Path | str | None = None, *,
                           warn_sink: Callable[[str], None] | LogSink | None = None
                           ) -> DimensionReader:
        """读并校验 registry；配置缺失/损坏时返回降级 reader（业务不会 500）。"""
        try:
            return cls(RegistryLoader().from_file(path), warn_sink=warn_sink)
        except (OSError, ValueError) as e:
            return cls(None, warn_sink=warn_sink, degraded_reason=str(e))

    def _warn_once(self, msg: str) -> None:
        if msg in type(self)._warned:
            return
        type(self)._warned.add(msg)
        sink = self._warn_sink
        if hasattr(sink, "warning"):
            sink.warning(msg)  # logging.Logger 等对象
        else:
            sink(msg)

    def _registry_or_none(self, subject: str) -> Registry | None:
        if self._registry is None:
            self._warn_once(
                f"registry 配置读取失败（{self._degraded_reason}），{subject} 暂用空映射")
            return None
        return self._registry

    def _query(self, table: str, columns: Sequence[str], *,
               order_by_rowid: bool = False, subject: str = "") -> list[Row] | None:
        """查一次全表；失败返回 None 由调用方降级为 {}。不抛异常给业务。"""
        repo = self._repository_arg
        owns = repo is None
        if owns:
            repo = SqliteDimensionRepository(self._registry, readonly=True)  # type: ignore[arg-type]
        try:
            return repo.query(table, columns, order_by_rowid=order_by_rowid)
        except sqlite3.Error as e:
            self._warn_once(
                f"读取 {table} 失败（{e}），{subject} 暂用空映射；"
                "若库文件不存在或表结构已变更，请重新导入维表库")
            return None
        finally:
            if owns:
                repo.close()

    def load_lookup(self, table_name: str) -> dict:
        """读取注册表声明的 lookup 映射；任何异常场景返回 ``{}``。"""
        reg = self._registry_or_none(table_name)
        if reg is None:
            return {}
        spec = reg.table(table_name)
        if spec is None:
            self._warn_once(f"未注册的表 {table_name}")
            return {}
        lk = spec.lookup
        if lk is None:
            self._warn_once(f"表 {table_name} 未声明 lookup")
            return {}

        rows = self._query(table_name, lk.select_columns, subject=table_name)
        if rows is None:
            return {}

        def clean(v):
            return "" if v is None else v

        if lk.is_multi:
            return {str(r[0]): tuple(clean(v) for v in r[1:]) for r in rows}
        return {str(r[0]): clean(r[1]) for r in rows}

    def load_product_selling_point(self) -> dict:
        """卖点维表：``{business_id: (product_name, selling_point, label_id)}``。

        元组顺序由 registry 的 ``lookup.values`` 决定，业务侧按 ``[0]/[1]/[2]``
        取值；``[2]`` 为含换行的标签 ID 原文，由业务侧自行 split。
        """
        return self.load_lookup(_PRODUCT_TABLE)

    def load_cut_in_script(self) -> dict:
        """切入/挽留话术：``{business_id: {标签ID / 兜底话术: [切入, 挽留] 或 ""}}``。

        库里只存「兜底话术」「匹配结果2」「标签0…标签N」原始列，拆解加工由
        ``transform`` 在读取侧完成。列名、标签列顺序、兜底键全部取自 registry。
        """
        reg = self._registry_or_none("cut_in_script")
        if reg is None:
            return {}
        spec = reg.table(_PRODUCT_TABLE)
        if spec is None:
            self._warn_once(f"未注册的表 {_PRODUCT_TABLE}")
            return {}
        ci = spec.cut_in_script
        if ci is None:
            self._warn_once(f"{_PRODUCT_TABLE} 未声明 cut_in_script")
            return {}

        transformer = CutInScriptTransformer(ci)
        rows = self._query(_PRODUCT_TABLE, transformer.select_columns,
                           order_by_rowid=True, subject="cut_in_script")
        if rows is None:
            return {}
        return transformer.build(rows)


__all__ = ["DimensionReader"]
