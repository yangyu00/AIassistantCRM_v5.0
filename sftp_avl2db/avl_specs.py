# -*- coding: utf-8 -*-
"""AVL 声明：一个 AVL = 一个 db，没有「库表形式模式」。

- ``name``    AVL 文件前缀，同时是 db 文件名（``{name}.db``）、CLI ``--task`` 值、
              在线查询**唯一的定位键**，也是**默认表名**
- ``project`` 项目 / 业务名，**只决定本地文件与 db 的输出目录**；同一业务的 AVL
              落在同一对文件夹里，靠文件名体现归属
- ``schema``  ``table_schemas.TABLE_SCHEMAS`` 的 key，多个 AVL 可复用同一份
- ``sharded`` 该 db 内是否按 product_no 分表；无论分不分，都只有一个 db 文件
- ``where`` / ``order_by`` 在线查询声明

**不声明表名**：固定表就叫 ``name``，分表叫 ``{name}_{分表号}``，由
``table_name()`` 统一派生，写入侧与读取侧共用这一条规则。

**产品 ID（product_id）不在这里，也不在读取侧**：它属于业务路由，由调用方
（``main.py``）维护 ``product_id → AVL 前缀`` 的映射后，把 AVL 前缀传进来。
本模块纯静态、零 I/O；``validate_specs()`` 在启动时拦配置漂移。
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Final, Mapping

# product_no 在两种逻辑记录里都是第 3 列；解析、分表、校验共用这一个名字。
PRODUCT_NO_INDEX: Final = 2


def shard_index(product_no, num_tables: int) -> int:
    """分表号：读写两侧唯一口径（``zlib.crc32(...) % NUM_TABLES``）。

    空主键取字符串 ``"NULL"``；解析器已跳过空主键，属不可达分支但口径保留。
    """
    p_no = product_no if product_no else "NULL"
    return zlib.crc32(str(p_no).encode("utf-8")) % num_tables


def table_name(base: str, shard: int | None = None) -> str:
    """库内表名的唯一口径：固定表 = AVL 名；分表 = ``{AVL 名}_{分表号}``。

    ``base`` 默认传 AVL 名；显式传别的值仅用于单测覆盖表名。
    """
    return base if shard is None else f"{base}_{shard}"


@dataclass(frozen=True)
class AvlSpec:
    """一个 AVL 的完整静态声明；不含产品 ID，也不含表名。"""

    name: str
    project: str
    schema: str                       # table_schemas.py 里的名字
    parser: str                       # "flat" | "continuation"
    sharded: bool = False
    where: tuple[str, ...] = ("product_no",)
    order_by: str | None = "op_time DESC"


AVL_SPECS: Final[Mapping[str, AvlSpec]] = {
    "F09554": AvlSpec(
        name="F09554", project="opportunity_recall",
        schema="opportunity_recall", parser="flat",
    ),
    "F09555": AvlSpec(
        name="F09555", project="opportunity_recall",
        schema="opportunity_recall", parser="flat",
    ),
    "F09556": AvlSpec(
        name="F09556", project="opportunity_recall",
        schema="opportunity_recall", parser="flat",
    ),
    "F09591": AvlSpec(
        name="F09591", project="loss_control",
        schema="loss_control", parser="continuation", sharded=True,
    ),
}


def spec_of(name: str) -> AvlSpec:
    """按 AVL 名取声明；未注册直接报错。"""
    try:
        return AVL_SPECS[name]
    except KeyError as e:
        raise KeyError(f"未注册的 AVL {name!r}；请在 avl_specs.py 中声明") from e


def validate_specs(profiles: Mapping[str, object]) -> None:
    """配置漂移在启动时暴露：AVL 集合、项目归属、表结构、分表声明逐一核对。"""
    from .table_schemas import TABLE_SCHEMAS, schema_columns

    names = set(profiles)
    if names != set(AVL_SPECS):
        raise ValueError(
            "AVL_SPECS 的 key 必须与 load_profiles() 产出的 AVL 完全一致；"
            f"spec={sorted(AVL_SPECS)} profiles={sorted(names)}")

    for name, spec in AVL_SPECS.items():
        conf = profiles[name]
        if spec.name != name:
            raise ValueError(f"{name}: AvlSpec.name 与声明 key 不一致")
        if getattr(conf, "name", None) != name:
            raise ValueError(f"{name}: profile.name 与声明 key 不一致")
        if getattr(conf, "project", None) != spec.project:
            raise ValueError(f"{name}: profile.project 与 AvlSpec.project 不一致")
        if getattr(conf, "sharded", None) != spec.sharded:
            raise ValueError(f"{name}: profile.sharded 与 AvlSpec.sharded 不一致")
        if spec.schema not in TABLE_SCHEMAS:
            raise ValueError(f"{name}: 表结构 {spec.schema} 未在 table_schemas.py 注册")
        if tuple(tuple(c) for c in getattr(conf, "schema", ())) != tuple(
                TABLE_SCHEMAS[spec.schema]):
            raise ValueError(f"{name}: profile.schema 与 TABLE_SCHEMAS 不一致")

        cols = schema_columns(spec.schema)
        for col in spec.where:
            if col not in cols:
                raise ValueError(f"{name}: where 列 {col} 不在表结构 {spec.schema} 中")
            # 查询键恒为 product_no：产品 ID 等业务字段不进数据访问层。
            if col != "product_no":
                raise ValueError(
                    f"{name}: 在线查询只按 product_no 定位，{col} 属业务侧条件，"
                    "请在 main.py 的路由表处理")

        # 表名恒由 name 派生，profile 里不再有 table_name / table_name_prefix。
        num_tables = getattr(conf, "num_tables", 0)
        if spec.sharded and num_tables <= 0:
            raise ValueError(f"{name}: 分表 AVL 需要 NUM_TABLES")
        if not spec.sharded and num_tables != 0:
            raise ValueError(f"{name}: 固定表 AVL 不应配置 NUM_TABLES")
        if getattr(conf, "parser", None) != spec.parser:
            raise ValueError(f"{name}: profile.parser 与 AvlSpec 不一致")


__all__ = [
    "PRODUCT_NO_INDEX", "shard_index", "table_name", "AvlSpec", "AVL_SPECS",
    "spec_of", "validate_specs",
]
