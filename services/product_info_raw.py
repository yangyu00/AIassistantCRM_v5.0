# -*- coding: utf-8 -*-
"""产品对象的最小公共形状 + 商机专属的辅助画像组件。"""
from __future__ import annotations


def build_auxiliary_portrait(queried_label_nvs):
    """按标签原本的名称与值逐项拼接，不做名称映射或按标签 ID 的值转换。"""
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
    """所有产品对象的最小公共形状（不含任何业务数据依赖）。"""

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
    """商机回捞专属：维表行 + IOP 标签，按需拼辅助画像。"""

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
        """按标签名值拼画像。"""
        if self._portrait is None:
            self._portrait = build_auxiliary_portrait(self.queried_label_nvs)
        return self._portrait


class ProductInfoRaw(ProductInfoBase):
    """第 4 类兜底对象（``code: "2"``）：不取数、不拼画像。"""

    def __init__(self, product_id, selling_point_rows=None):
        row = (selling_point_rows or {}).get(product_id)
        name = getattr(row, "product_name", None) if row is not None else None
        super().__init__(product_id, product_name=name, output_script="无")


__all__ = ["ProductInfoBase", "AuxiliaryPortrait", "ProductInfoRaw",
           "build_auxiliary_portrait"]
