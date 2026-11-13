import re

from services.product_info_raw import ProductInfoRaw
from src import consts

from src.log_util import my_log


class ProductInfoRewrite(ProductInfoRaw):
    def __init__(self, product_id, custom_label_info):
        super().__init__(product_id, custom_label_info)
        self.selling_point = self.__get_selling_point()
        self.cut_in_statement = self.__get_cut_in_statement()
        self.retain_statement = self.__get_retain_statement()
        self.output_script = self.__get_output_script()

    def __get_selling_point(self):
        if self.product_id not in self.product_selling_point_match_dict:
            return ""

        old_text = self.product_selling_point_match_dict[self.product_id][1]
        # 将“1、资费：”后的字符标红，若“2、价格”未换行，增加换行符
        pattern = re.compile(r'\d、.*?：', re.DOTALL)
        extract = pattern.findall(old_text)
        if not extract:
            return "【卖点】\n" + old_text

        # 此时至少存在"1、xxx："
        start_index = old_text.find(extract[0]) + len(extract[0])

        # 此时只有"1、xxx："
        if len(extract) == 1:
            return "【卖点】\n" + '<strong>' + old_text[:start_index] + '<span style="color:red;">' + old_text[
                                                                                                    start_index:] + '</span></strong>'

        # 此时至少存在"2、xxx："
        end_index = old_text.find(extract[1])
        fusion_text = "【卖点】\n" + '<strong>' + old_text[:start_index] + '<span style="color:red;">' + old_text[
                                                                                                       start_index:end_index].strip() + '</span></strong>'
        ready_join_text = old_text[end_index:]
        split_chars_list = []
        for split_chars in extract[1:]:
            split_chars_list.append(split_chars)
        split_rule = f'{"|".join(re.escape(char) for char in split_chars_list)}'
        for i, g in enumerate(re.split(split_rule, ready_join_text)[1:]):
            fusion_text += "\n" + '<strong>' + split_chars_list[i] + '</strong>' + g.strip()

        return fusion_text

    def __get_cut_in_statement(self):
        if (self.product_id not in consts.cut_in_script_dict.keys()
                or not isinstance(consts.cut_in_script_dict[self.product_id], dict)
                or not consts.cut_in_script_dict[self.product_id]):
            return ""

        id_sequence_obj = consts.cut_in_script_dict[self.product_id]

        fallback_script = ""

        try:
            for search_id, fusion_script_list in id_sequence_obj.items():
                if (search_id != "兜底话术"
                        and search_id in self.custom_label_info.keys()
                        and (self.custom_label_info[search_id][1] == "是" or self.custom_label_info[search_id][
                            1] == "1")
                        and fusion_script_list[0]):
                    return fusion_script_list[0]
            else:
                if "兜底话术" in id_sequence_obj.keys():
                    fusion_script_list = id_sequence_obj["兜底话术"]
                    if isinstance(fusion_script_list, list):
                        fallback_script = fusion_script_list[0]

        except Exception as e:
            my_log.error(f'切入话术读取错误：{str(e)}')

        return fallback_script

    def __get_retain_statement(self):
        if (self.product_id not in consts.cut_in_script_dict.keys()
                or not isinstance(consts.cut_in_script_dict[self.product_id], dict)
                or not consts.cut_in_script_dict[self.product_id]):
            return ""

        id_sequence_obj = consts.cut_in_script_dict[self.product_id]

        fallback_script = ""

        try:
            for search_id, fusion_script_list in id_sequence_obj.items():
                if (search_id != "兜底话术"
                        and search_id in self.custom_label_info.keys()
                        and (self.custom_label_info[search_id][1] == "是" or self.custom_label_info[search_id][
                            1] == "1")
                        and fusion_script_list[1]):
                    return fusion_script_list[1]
            else:
                if "兜底话术" in id_sequence_obj.keys():
                    fusion_script_list = id_sequence_obj["兜底话术"]
                    if isinstance(fusion_script_list, list):
                        fallback_script = fusion_script_list[1]

        except Exception as e:
            my_log.error(f'挽留话术读取错误：{str(e)}')

        return fallback_script

    @staticmethod
    def __strong_text(text):
        need_strong_text = ["【辅助画像】", "【切入话术】", "【卖点】", "【挽留话术】"]
        for i in need_strong_text:
            text = text.replace(i, f"<strong>{i}</strong>")

        return text

    def __get_output_script(self):
        # 维表中业务
        output_script = ""
        output_script += (self.auxiliary_portrait + '\n') if self.auxiliary_portrait else ""
        output_script += (self.cut_in_statement + '\n') if self.cut_in_statement else ""
        output_script += (self.selling_point + '\n') if self.selling_point else ""
        output_script += self.retain_statement if self.retain_statement else ""

        output_script = self.__strong_text(output_script)

        return output_script

    def __str__(self):
        return (f'Product ID: {self.product_id}\n'
                f'Product Name: {self.product_name}\n'
                f'queried_label_ids: {self.queried_label_ids}\n'
                f'queried_label_nvs: {self.queried_label_nvs}\n'
                f'selling_point: {self.selling_point}\n'
                f'auxiliary_portrait: {self.auxiliary_portrait}\n'
                f'cut_in_statement: {self.cut_in_statement}\n'
                f'retain_statement: {self.retain_statement}\n'
                f'output_script: {self.output_script}')
