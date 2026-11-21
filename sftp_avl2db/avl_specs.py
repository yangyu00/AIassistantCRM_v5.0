# -*- coding: utf-8 -*-
"""AVL 声明：一个 AVL = 一个 db。

- ``name``    AVL 文件前缀，同时是 db 文件名（``{name}.db``）、CLI ``--task`` 值、
              在线查询**唯一的定位键**，也是**默认表名**
- ``project`` 项目 / 业务名，**只决定本地文件与 db 的输出目录**；同一业务的 AVL
              落在同一对文件夹里，靠文件名体现归属
- ``schema``  ``table_schemas.TABLE_SCHEMAS`` 的 key，多个 AVL 可复用同一份
- ``sharded`` 该 db 内是否按 product_no 分表；无论分不分，都只有一个 db 文件
- ``where`` / ``order_by`` 在线查询声明。商机回捞 / 控损按 ``product_no``；
              诊断新线里只有 ``F09633``（用户场景，千万级行）含手机号列，也按
              ``product_no`` 查并据此分表；其余 7 张维表按各自业务定位键声明
              （必须是表内列、且属于主键列，由 ``validate_specs()`` 拦漂移）

表名由 ``table_name()`` 统一派生：固定表叫 ``name``，分表叫
``{name}_{分表号}``，写入侧与读取侧共用这一条规则。

主键按 schema 名集中注册在 ``table_schemas.TABLE_PRIMARY_KEYS``，
``config.load_profiles()`` 写进 profile，建表 DDL 与解析层共用。

产品 ID 属业务路由：商机 / 控损的 ``product_id → AVL 前缀`` 映射在
``main.py``，诊断新线按 ``F09629`` 动态命中。调用方把 AVL 前缀传进来即可。
本模块纯静态、零 I/O；``validate_specs()`` 在启动时拦配置漂移。
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Final, Mapping

# 分表路由用的 product_no 列下标由 build_layout() 从注册的表结构推导；
# 这里保留固定值，仅作为不分表 AVL / 单测显式构造 layout 时的默认值。
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

    # ============ 诊断新线（project = diagnosis，8 个 AVL 各一个 db） ============
    # where = 该表业务定位键（PDF 取数口径）；全部是主键列的子集，保证查询
    # 命中唯一键或主键前缀。多行 / 多条件取数走 services.reader.query_many()，
    # 场景交集与话术拼装见 services/diagnosis/。
    "F09627": AvlSpec(                       # DIM_MARKET_SCENE_RANK_ONCE
        name="F09627", project="diagnosis",
        schema="scene_rank", parser="flat", where=("scene",),
    ),
    "F09628": AvlSpec(                       # DIM_MARKET_PRODUCT_SCENE_ONCE
        name="F09628", project="diagnosis",
        schema="product_scene", parser="flat", where=("business_type",),
    ),
    "F09629": AvlSpec(                       # DIM_MARKET_PRODUCT_BUSINESS_ONCE
        name="F09629", project="diagnosis",
        schema="product_business", parser="flat", where=("product_id",),
    ),
    "F09630": AvlSpec(                       # DIM_MARKET_SCENE_QRHUASHU_ONCE
        name="F09630", project="diagnosis",
        schema="scene_qrhuashu", parser="flat", where=("scene",),
    ),
    "F09631": AvlSpec(                       # DIM_MARKET_PRODUCT_YXHUASHU_ONCE
        name="F09631", project="diagnosis",
        schema="product_yxhuashu", parser="flat", where=("business_type",),
    ),
    "F09632": AvlSpec(                       # DIM_MARKET_PRODUCT_WLHUASHU_ONCE
        name="F09632", project="diagnosis",
        schema="product_wlhuashu", parser="flat", where=("business_type",),
    ),
    "F09633": AvlSpec(                       # DWD_MARKET_CONF_USER_SCENE_DS
        name="F09633", project="diagnosis",
        # 千万级用户场景表：按 product_no 分表。分表键与在线查询键（telnum）
        # 完全一致，读取侧可精确定位单表，不需要广播到全部分表。
        schema="conf_user_scene", parser="flat", sharded=True,
        where=("product_no",),
    ),
    "F09635": AvlSpec(                       # TAGID_TAGNAME（xls 未给 DDL）
        name="F09635", project="diagnosis",
        schema="tagid_tagname", parser="flat", where=("tag_id",),
    ),
}


def spec_of(name: str) -> AvlSpec:
    """按 AVL 名取声明；未注册直接报错。"""
    try:
        return AVL_SPECS[name]
    except KeyError as e:
        raise KeyError(f"未注册的 AVL {name!r}；请在 avl_specs.py 中声明") from e


def validate_specs(profiles: Mapping[str, object]) -> None:
    """配置漂移在启动时暴露：AVL 集合、项目归属、表结构、主键、分表声明逐一核对。"""
    from .table_schemas import TABLE_SCHEMAS, schema_columns, schema_primary_key

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
        pk = schema_primary_key(spec.schema)
        if not pk or any(col not in cols for col in pk):
            raise ValueError(
                f"{name}: 主键 {pk} 含不在表结构 {spec.schema} 中的列")
        if tuple(getattr(conf, "primary_key", ())) != tuple(pk):
            raise ValueError(f"{name}: profile.primary_key 与注册主键不一致")

        if not spec.where:
            raise ValueError(f"{name}: where 不能为空")
        for col in spec.where:
            if col not in cols:
                raise ValueError(f"{name}: where 列 {col} 不在表结构 {spec.schema} 中")
            # 查询键必须落在主键列上：商机回捞 / 控损是 product_no，
            # 诊断维表是各自的业务定位键；非主键的业务条件一律留在 main.py。
            if col not in pk:
                raise ValueError(
                    f"{name}: 查询键 {col} 不是主键列；业务侧条件请在 main.py 处理")

        # 表名恒由 name 派生，读写两侧共用 table_name()。
        # 分表 AVL 必须以 product_no 为分表键，且查询声明里也要带它：
        # 否则读取侧无法定位分表，会退化成全表广播或直接查不到数据。
        if spec.sharded:
            if "product_no" not in cols:
                raise ValueError(
                    f"{name}: 分表 AVL 的表结构必须含 product_no 分表键")
            if "product_no" not in spec.where:
                raise ValueError(
                    f"{name}: 分表 AVL 的 where 必须含 product_no，"
                    "否则读取侧无法定位分表")
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
