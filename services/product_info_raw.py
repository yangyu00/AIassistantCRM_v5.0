# -*- coding: utf-8 -*-
"""产品对象的最小公共形状 + 商机专属的辅助画像组件。

四类对象里只有商机回捞消费 IOP 标签、卖点维表标签 ID 子集与辅助画像：
控损只消费 ``record['strategy_name']``；诊断新线的 IOP 标签与维表行在
``build_diagnosis_record()`` 取数阶段就已用掉，对象只消费结构化 ``record``；
兜底对象什么数据都不需要。

因此拆成三层：

- ``ProductInfoBase``：**极简公共基类**，只保留所有对象都有的
  ``product_id`` / ``product_name`` / ``output_script`` 与统一的日志输出；
- ``AuxiliaryPortrait``：**商机专属组件**（组合，不是继承），持有 IOP 标签与
  卖点维表行映射 ``selling_point_rows``，按需计算画像并把结果缓存在实例上；
- ``ProductInfoRaw``：第 4 类兜底对象，只继承 ``ProductInfoBase``。

**「是否生成画像」不是一个开关**：只有商机对象会创建 ``AuxiliaryPortrait``；
维表未命中 / 标签子集为空 / IOP 降级为 ``{}`` 时，``build_auxiliary_portrait()``
自然返回空串，商机话术里的 ``if portrait`` 就不会追加。编排层不需要传缓存，
对象工厂也不需要按业务分发一个画像开关。旧的
``product_selling_point_match_dict`` 属性名（既不像字典内容、也不像它的用途）
已删除。
"""
from __future__ import annotations


def build_auxiliary_portrait(queried_label_nvs):
    """按标签原本的名称与值逐项拼接，不做名称映射或按标签 ID 的值转换。

    ``queried_label_nvs`` 由 ``AuxiliaryPortrait`` 按维表给出的标签 ID 顺序与
    ``custom_label_info`` 对齐后传入；函数只读入参、不访问对象状态。空列表或
    全空值直接返回空串，这就是「不生成画像」的唯一路径。
    """
    portrait_str = ""
    for label_nv in queried_label_nvs:
        if label_nv is None:
            continue
        label_name, label_value = label_nv[0], label_nv[1]
        if not label_value:
            continue
        portrait_str += f'{label_name}: <strong><span style="color:blue;">{label_value}</span></strong>; '

    if portrait_str:
        portrait_str = "【辅助画像】" + portrait_str
        portrait_str = portrait_str.rstrip('; ')
    return portrait_str


class ProductInfoBase(object):
    """所有产品对象的最小公共形状（不含任何业务数据依赖）。

    子类只需在 ``__init__`` 里给出自己的 ``product_name`` / ``output_script``，
    需要往日志里追加字段时覆写 ``_extra_str_lines()``；公共三行由基类统一输出，
    不再每个子类复制一遍。
    """

    def __init__(self, product_id, product_name=None, output_script="无"):
        self.product_id = product_id
        self.product_name = product_name
        self.output_script = output_script

    def _extra_str_lines(self):
        """子类追加的日志行（排在 ``output_script`` 之前）。"""
        return []

    def __str__(self):
        lines = [f'Product ID: {self.product_id}',
                 f'Product Name: {self.product_name}']
        lines.extend(self._extra_str_lines())
        lines.append(f'output_script: {self.output_script}')
        return "\n".join(lines)


class AuxiliaryPortrait(object):
    """商机回捞专属：维表行 + IOP 标签，按需拼辅助画像。

    **组合而非继承**：只有 ``ProductInfoByOpportunityRecall`` 会创建它，其它业务
    对象连这些字段都不存在。``selling_point_rows`` 是
    ``{product_id: SellingPointRow}``（由 ``dimensions.load_selling_points()``
    在请求开始时批量取回），替代旧名 ``product_selling_point_match_dict``。

    ``_portrait`` 是**实例级**懒缓存：同一对象的话术拼装与 ``__str__`` 日志会各
    读一次 ``auxiliary_portrait``，只算一遍即可。它不跨对象、不跨请求共享，
    也不由 ``main.py`` 传进来——画像只是一次纯字符串拼接，没有需要跨对象
    复用的 I/O 结果。
    """

    def __init__(self, product_id, custom_label_info=None,
                 selling_point_rows=None):
        self.product_id = product_id
        self.custom_label_info = custom_label_info or {}
        self.selling_point_rows = selling_point_rows or {}
        self._portrait = None
        self._label_ids = None
        self._label_nvs = None

    @property
    def row(self):
        return self.selling_point_rows.get(self.product_id)

    @property
    def product_name(self):
        row = self.row
        return getattr(row, "product_name", None) if row is not None else None

    @property
    def queried_label_ids(self):
        """标签 ID 子集来自维表该行「标签ID」，顺序已在维表侧定好（懒计算）。"""
        if self._label_ids is None:
            row = self.row
            self._label_ids = (list(getattr(row, "label_ids", ()))
                               if row is not None else [])
        return self._label_ids

    @property
    def queried_label_nvs(self):
        """按 ``queried_label_ids`` 顺序与 IOP 返回对齐，缺失填 ``None``（懒计算）。"""
        if self._label_nvs is None:
            nvs = []
            for label_id in self.queried_label_ids:
                nv = self.custom_label_info.get(label_id)
                # IOP 异常返回（非二元组）按缺失处理，不抛 IndexError
                if isinstance(nv, (tuple, list)) and len(nv) >= 2:
                    nvs.append((nv[0], nv[1]))
                else:
                    nvs.append(None)
            self._label_nvs = nvs
        return self._label_nvs

    @property
    def auxiliary_portrait(self):
        """按标签名值拼画像；空输入自然返回 ``""``，不设显式开关。"""
        if self._portrait is None:
            self._portrait = build_auxiliary_portrait(self.queried_label_nvs)
        return self._portrait


class ProductInfoRaw(ProductInfoBase):
    """第 4 类兜底对象（``code: "2"``）：不取数、不拼画像。

    仅按 ``product_id`` 从卖点维表行映射里取一个名称用于日志（响应层对兜底
    强制写 ``productName=""``，按对象类型判 code，不靠名称是否为空）。
    旧版它同时扮演「公共基类」，现已拆开。
    """

    def __init__(self, product_id, selling_point_rows=None):
        row = (selling_point_rows or {}).get(product_id)
        name = getattr(row, "product_name", None) if row is not None else None
        super().__init__(product_id, product_name=name, output_script="无")


__all__ = ["ProductInfoBase", "AuxiliaryPortrait", "ProductInfoRaw",
           "build_auxiliary_portrait"]
