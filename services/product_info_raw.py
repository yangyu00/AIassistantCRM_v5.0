from excel2consts.dim_reader import load_product_selling_point

# 商机回捞的标签子集固定，不随卖点维表变动。号卡 / 套餐共用 3 个，宽带 4 个；
# 元组顺序即辅助画像的拼接顺序。其余产品 ID 仍按维表 label_id 列取。
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


class ProductInfoRaw(object):
    def __init__(self, product_id, custom_label_info):
        self.product_id = product_id
        self.custom_label_info = custom_label_info
        # 一次请求内只读一次；维表业务下线后仅兜底与日志还会用到，读取时机
        # 将在后续统一收敛
        self.product_selling_point_match_dict = load_product_selling_point()
        self.product_name = self.__get_product_name()
        self.queried_label_ids = self.__get_queried_label_ids()
        self.queried_label_nvs = self.__get_queried_label_nvs()
        self.auxiliary_portrait = self.__get_auxiliary_portrait()
        self.output_script = "无"

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

    def __get_auxiliary_portrait(self):
        """按标签原本的名称与值逐项拼接，不做名称映射或按标签 ID 的值转换。

        商机回捞的标签子集固定，这 5 个标签的名称直接取上游返回值即可。
        """
        portrait_str = ""
        exist_label_name_list = []
        for label_nv in self.queried_label_nvs:
            if label_nv is None:
                continue
            label_name, label_value = label_nv[0], label_nv[1]
            if not label_value:
                continue

            # 涉及是否的重复标签去重
            if label_name in exist_label_name_list:
                if label_value == "是":
                    portrait_str.replace(f"{label_name}: 否; ", f"{label_name}: 是; ")
                    continue
                else:
                    continue

            portrait_str += f'{label_name}: <strong><span style="color:blue;">{label_value}</span></strong>; '
            exist_label_name_list.append(label_name)

        if portrait_str:
            portrait_str = "【辅助画像】" + portrait_str
            portrait_str = portrait_str.rstrip('; ')

        return portrait_str

    def __str__(self):
        return (f'Product ID: {self.product_id}\n'
                f'Product Name: {self.product_name}\n'
                f'queried_label_ids: {self.queried_label_ids}\n'
                f'queried_label_nvs: {self.queried_label_nvs}\n'
                f'auxiliary_portrait: {self.auxiliary_portrait}')
