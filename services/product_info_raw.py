# -*- coding: utf-8 -*-
"""产品对象公共基类：名称、标签子集、辅助画像的拼装。

标签子集 **不再硬编码**：由在线侧 ``services.dimensions`` 从卖点维表
（``local_excel2db`` 的 ``selling_points`` 表「标签ID」列）按 ``product_id``
查出，在请求开始时一次性传入。基类只消费传入的映射，不读库、不读配置。

``custom_label_info`` 仍是 IOP 返回的 ``{label_id: (标签名, 标签值)}``：
维表决定「查哪些 ID」，IOP 决定「这些 ID 的名称与值」。
"""
from __future__ import annotations


def build_auxiliary_portrait(queried_label_nvs):
    """按标签原本的名称与值逐项拼接，不做名称映射或按标签 ID 的值转换。

    ``queried_label_nvs`` 由基类按卖点维表给出的标签 ID 顺序与
    ``custom_label_info`` 对齐后传入；函数只读入参、不访问对象状态，
    只在声明需要画像的业务分支调用，结果按 ``product_id`` 在一次请求内复用。
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


class ProductInfoRaw(object):
    def __init__(self, product_id, custom_label_info, selling_points, portrait_cache=None):
        self.product_id = product_id
        self.custom_label_info = custom_label_info
        # 卖点维表映射 {product_id: SellingPointRow}，由调用方在一次请求开始时
        # 从 dimensions.load_selling_points() 读入并传入，避免每个对象各读一次库。
        # 维表未命中 / 库缺失时该 dict 为空，标签子集自然为空、画像为空。
        self.product_selling_point_match_dict = selling_points or {}
        self.product_name = self.__get_product_name()
        self.queried_label_ids = self.__get_queried_label_ids()
        self.queried_label_nvs = self.__get_queried_label_nvs()
        # portrait_cache 是调用方在一次请求内创建的空 dict，按 product_id 复用
        # 画像结果：同一 ID 在列表里出现多次也只拼一次。传 None 表示该对象
        # 不需要画像，连计算都不发生。缓存只在函数内创建、不跨请求共享。
        self._portrait_cache = portrait_cache
        self.output_script = "无"

    @property
    def auxiliary_portrait(self):
        if self._portrait_cache is None:
            return ""
        key = self.product_id
        if key not in self._portrait_cache:
            self._portrait_cache[key] = build_auxiliary_portrait(self.queried_label_nvs)
        return self._portrait_cache[key]

    def __get_product_name(self):
        row = self.product_selling_point_match_dict.get(self.product_id)
        return getattr(row, "product_name", None) if row is not None else None

    def __get_queried_label_ids(self):
        """标签 ID 子集来自卖点维表该行的「标签ID」列，顺序已在维表侧定好。"""
        row = self.product_selling_point_match_dict.get(self.product_id)
        return list(getattr(row, "label_ids", ())) if row is not None else []

    def __get_queried_label_nvs(self):
        queried_label_nvs = []
        for one_label_id in self.queried_label_ids:
            if one_label_id not in self.custom_label_info:
                queried_label_nvs.append(None)
                continue
            queried_label_nvs.append((self.custom_label_info[one_label_id][0],
                                      self.custom_label_info[one_label_id][1]))

        return queried_label_nvs

    def __str__(self):
        return (f'Product ID: {self.product_id}\n'
                f'Product Name: {self.product_name}\n'
                f'queried_label_ids: {self.queried_label_ids}\n'
                f'queried_label_nvs: {self.queried_label_nvs}\n'
                f'auxiliary_portrait: {self.auxiliary_portrait}')
