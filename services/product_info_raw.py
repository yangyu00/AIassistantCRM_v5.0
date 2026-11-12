from src import consts


class ProductInfoRaw(object):
    def __init__(self, product_id, custom_label_info):
        self.product_id = product_id
        self.custom_label_info = custom_label_info
        self.product_name = self.__get_product_name()
        self.queried_label_ids = self.__get_queried_label_ids()
        self.queried_label_nvs = self.__get_queried_label_nvs()
        self.auxiliary_portrait = self.__get_auxiliary_portrait()
        self.output_script = "无"

    def __get_product_name(self):
        if self.product_id not in consts.product_selling_point_match_dict:
            return None
        return consts.product_selling_point_match_dict[self.product_id][0]

    def __get_queried_label_ids(self):
        if self.product_id not in consts.product_selling_point_match_dict:
            return []

        return consts.product_selling_point_match_dict[self.product_id][2].split("\n")

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
        portrait_str = ""
        exist_label_name_list = []
        for i, label_nv in enumerate(self.queried_label_nvs):
            if label_nv is None:
                continue
            label_name, label_value = label_nv[0], label_nv[1]
            if not label_value:
                continue

            if self.queried_label_ids[i] in consts.label_name_mapping.keys():
                label_name = consts.label_name_mapping[self.queried_label_ids[i]]

            # 近期上网掉线、卡顿记录的特殊处理
            if self.queried_label_ids[i] == "a1e357f57b474d51119bf003443bd088" and str(label_value) == "0.00":
                label_value = "无"

            # 涉及费用的标签转换规则
            if self.queried_label_ids[i] == "cb933e7eb32b67ec637c78c6f2be65a1":
                if str(label_value) == "0.00":
                    label_value = "0元"
                else:
                    label_value = str(label_value) + "元"

            # 抖音快手使用流量后加“M”单位
            if self.queried_label_ids[i] in ("44aad5383d8d8910e72bd8f74f1cd1df", "bbc3b62f605a0112d7bde076eb24fea5"):
                label_value = str(label_value) + "M"

            # 1,0 切换成 是, 否
            if "是否" in label_name and str(label_value) == "1":
                label_value = "是"
            elif "是否" in label_name and str(label_value) == "0":
                label_value = "否"
            if self.queried_label_ids[i] == "1c7b8fdc55e00c4300adcf1c0543f00f":
                label_value = ("是" if str(label_value) == "1" else "否")

            # 涉及是否的重复标签去重
            if label_name in exist_label_name_list:
                if label_value == "是":
                    portrait_str.replace(f"{label_name}: 否; ", f"{label_name}: 是; ")
                    continue
                else:
                    continue

            # 降档阈值的特殊处理
            if self.queried_label_ids[i] in ("0e6f7d4a8b32aa551d09174481857c39", "efe5295c6763e2eab16afbaa984a9ee4"):
                portrait_str += f'<strong><span style="color:red;">{label_name}</span></strong>: {label_value}; '
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
