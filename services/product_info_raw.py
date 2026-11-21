# 商机回捞的标签子集固定。号卡 / 套餐共用 3 个，宽带 4 个；
# 元组顺序即辅助画像的拼接顺序。其余产品 ID 当前无维表来源，标签子集为空
# （在线侧传入的卖点映射恒为 {}，维表读取侧已下线）。
_OPP_LABEL_IDS = {
    "202608041615335173": (
        "5f21acac078c9604df4b1f8bb9d69628",
        "72c681b94c461faee3dee5b177a04996",
        "78cf3d36d08d5b65bab8da44dee1bf6d",
    ),
    "202608041615335172": (
        "5f21acac078c9604df4b1f8bb9d69628",
        "72c681b94c461faee3dee5b177a04996",
        "78cf3d36d08d5b65bab8da44dee1bf6d",
    ),
    "202608041615335174": (
        "5f21acac078c9604df4b1f8bb9d69628",
        "78cf3d36d08d5b65bab8da44dee1bf6d",
        "9bc591ba3d9bbe619ef167f5ffdde367",
        "ddd18dc77c4824d6d19bc1607f5bd286",
    ),
}


def build_auxiliary_portrait(queried_label_nvs):
    """按标签原本的名称与值逐项拼接，不做名称映射或按标签 ID 的值转换。

    queried_label_nvs 由调用方按产品 ID 预先取好；商机回捞的标签子集固定，
    这 5 个标签的名称直接取上游返回值即可。函数只读入参、不访问对象状态，
    只在声明需要画像的业务分支调用，结果按 product_id 在一次请求内复用。
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
        # 卖点维表由调用方在一次请求开始时读入并传入，避免每个产品对象各读一次
        self.product_selling_point_match_dict = selling_points
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
        if self.product_id not in self.product_selling_point_match_dict:
            return None
        return self.product_selling_point_match_dict[self.product_id][0]

    def __get_queried_label_ids(self):
        fixed = _OPP_LABEL_IDS.get(self.product_id)
        if fixed is not None:
            return list(fixed)

        if self.product_id not in self.product_selling_point_match_dict:
            return []

        return self.product_selling_point_match_dict[self.product_id][2].split("\n")

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
