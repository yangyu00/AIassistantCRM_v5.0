"""上一版本输出参照实现（仅用于 ``check_diagnosis_offline.py`` 的 golden 比对）。

逐行直译用户给出的四个历史文件：

* ``product_selling_point_match.py::cut_in_str2tuple``
* ``product_selling_point_match.py::rewrite_product_selling_point_match_dict``
  里两个 consts 字典的**构造形状**（输入换成 db 行，不再读 Excel）
* ``product_info_raw.py::ProductInfoRaw``（辅助画像）
* ``rewrite.py::ProductInfoRewrite``（卖点 / 切入 / 挽留 / 总拼装）

这个模块不参与线上运行、不被 ``services`` import，只提供一份独立的
「上一版本会输出什么」的答案，用来证明新模块的输出逐字符一致。
"""
from __future__ import annotations

import re

_STRONG_TITLES = ("【辅助画像】", "【切入话术】", "【卖点】", "【挽留话术】")


# ---- product_selling_point_match.py::cut_in_str2tuple ----------------------
def cut_in_str2tuple(fusion_scripts: str):
    def remake_statement(extract_chunk):
        statement_list = []
        for one_cut_in_statement in extract_chunk.splitlines():
            if not one_cut_in_statement:
                continue
            split_statement = one_cut_in_statement.strip().replace(':', '：').split("：")
            if len(split_statement) == 1:
                statement_list.append(split_statement[0])
            else:
                split_statement[0] = re.sub(r'^\d+\s?\.\s?', '', split_statement[0])
                split_statement[0] = ('<strong><span style="color: blue;">'
                                      + split_statement[0] + "："
                                      + '</span></strong>')
                statement_list.append(
                    split_statement[0] + '：'.join(split_statement[1:]))
        return '\n'.join(statement_list)

    injection_scripts = []
    pattern = re.compile(r'切入话术(.*?)拒绝挽留话术(.*)', re.DOTALL)
    extract = pattern.findall(fusion_scripts)
    if extract and len(extract[0]) == 2:
        cut_in_statement = remake_statement(extract[0][0].strip())
        cut_in_statement = ("【切入话术】\n" + cut_in_statement
                            if cut_in_statement else "")
        injection_scripts.append(cut_in_statement)
        retention_statement = remake_statement(extract[0][1].strip())
        retention_statement = ("【挽留话术】\n" + retention_statement
                               if retention_statement else "")
        injection_scripts.append(retention_statement)
    else:
        injection_scripts.append('')

    for one_content in injection_scripts:
        if one_content:
            break
    else:
        injection_scripts = ""
    return injection_scripts


def build_legacy_consts(product_id, row):
    """按上一版本两个 consts 字典的形状，从 db 一行构造等价数据。"""
    pspm = {
        product_id: (
            row.product_name,
            row.selling_point,
            "\n".join(row.label_ids),
        )
    }

    def parse(text):
        return cut_in_str2tuple(str(text or "").strip().replace("**", ""))

    inner_dict = {"兜底话术": parse(row.fallback_script)}
    match_result = str(row.match_result or "").strip()
    if match_result:
        id_group_list = match_result.split('\n')
        for g, one_id in enumerate(id_group_list):
            raw = (str(row.tag_texts[g]).strip()
                   if g < len(row.tag_texts) else "")
            inner_dict[one_id] = parse(raw) if raw else ""
    else:
        inner_dict = {}
    return pspm, {product_id: inner_dict}


# ---- product_info_raw.py::ProductInfoRaw -----------------------------------
def _auxiliary_portrait(queried_label_ids, queried_label_nvs, mapping):
    portrait_str = ""
    exist_label_name_list = []
    for i, label_nv in enumerate(queried_label_nvs):
        if label_nv is None:
            continue
        label_name, label_value = label_nv[0], label_nv[1]
        if not label_value:
            continue

        if queried_label_ids[i] in mapping.keys():
            label_name = mapping[queried_label_ids[i]]

        if (queried_label_ids[i] == "a1e357f57b474d51119bf003443bd088"
                and str(label_value) == "0.00"):
            label_value = "无"

        if queried_label_ids[i] == "cb933e7eb32b67ec637c78c6f2be65a1":
            label_value = ("0元" if str(label_value) == "0.00"
                           else str(label_value) + "元")

        if queried_label_ids[i] in ("44aad5383d8d8910e72bd8f74f1cd1df",
                                    "bbc3b62f605a0112d7bde076eb24fea5"):
            label_value = str(label_value) + "M"

        if "是否" in label_name and str(label_value) == "1":
            label_value = "是"
        elif "是否" in label_name and str(label_value) == "0":
            label_value = "否"
        if queried_label_ids[i] == "1c7b8fdc55e00c4300adcf1c0543f00f":
            label_value = ("是" if str(label_value) == "1" else "否")

        if label_name in exist_label_name_list:
            if label_value == "是":
                portrait_str.replace(f"{label_name}: 否; ",
                                     f"{label_name}: 是; ")
                continue
            continue

        if queried_label_ids[i] in ("0e6f7d4a8b32aa551d09174481857c39",
                                    "efe5295c6763e2eab16afbaa984a9ee4"):
            portrait_str += (f'<strong><span style="color:red;">'
                             f'{label_name}</span></strong>: {label_value}; ')
            continue

        portrait_str += (f'{label_name}: <strong><span style="color:blue;">'
                         f'{label_value}</span></strong>; ')
        exist_label_name_list.append(label_name)

    if portrait_str:
        portrait_str = "【辅助画像】" + portrait_str
        portrait_str = portrait_str.rstrip('; ')
    return portrait_str


# ---- rewrite.py::ProductInfoRewrite ----------------------------------------
def _selling_point(old_text):
    pattern = re.compile(r'\d、.*?：', re.DOTALL)
    extract = pattern.findall(old_text)
    if not extract:
        return "【卖点】\n" + old_text

    start_index = old_text.find(extract[0]) + len(extract[0])
    if len(extract) == 1:
        return ("【卖点】\n" + '<strong>' + old_text[:start_index]
                + '<span style="color:red;">' + old_text[start_index:]
                + '</span></strong>')

    end_index = old_text.find(extract[1])
    fusion_text = ("【卖点】\n" + '<strong>' + old_text[:start_index]
                   + '<span style="color:red;">'
                   + old_text[start_index:end_index].strip()
                   + '</span></strong>')
    ready_join_text = old_text[end_index:]
    split_chars_list = []
    for split_chars in extract[1:]:
        split_chars_list.append(split_chars)
    split_rule = f'{"|".join(re.escape(char) for char in split_chars_list)}'
    for i, g in enumerate(re.split(split_rule, ready_join_text)[1:]):
        fusion_text += ("\n" + '<strong>' + split_chars_list[i]
                        + '</strong>' + g.strip())
    return fusion_text


def _select(id_sequence_obj, custom_label_info, index):
    fallback_script = ""
    try:
        for search_id, fusion_script_list in id_sequence_obj.items():
            if (search_id != "兜底话术"
                    and search_id in custom_label_info.keys()
                    and (custom_label_info[search_id][1] == "是"
                         or custom_label_info[search_id][1] == "1")
                    and fusion_script_list[index]):
                return fusion_script_list[index]
        else:
            if "兜底话术" in id_sequence_obj.keys():
                fusion_script_list = id_sequence_obj["兜底话术"]
                if isinstance(fusion_script_list, list):
                    fallback_script = fusion_script_list[index]
    except Exception:
        return ""
    return fallback_script


def legacy_output(product_id, pspm, cisd, custom_label_info,
                  label_name_mapping=None):
    """返回上一版本的 ``(product_name, output_script)``。"""
    mapping = label_name_mapping or {}
    product_name = pspm[product_id][0]
    queried_label_ids = pspm[product_id][2].split("\n")

    queried_label_nvs = []
    for one_label_id in queried_label_ids:
        if one_label_id not in custom_label_info:
            queried_label_nvs.append(None)
            continue
        queried_label_nvs.append((custom_label_info[one_label_id][0],
                                  custom_label_info[one_label_id][1]))

    auxiliary_portrait = _auxiliary_portrait(
        queried_label_ids, queried_label_nvs, mapping)
    selling_point = _selling_point(pspm[product_id][1])
    id_sequence_obj = cisd[product_id]
    cut_in_statement = _select(id_sequence_obj, custom_label_info, 0)
    retain_statement = _select(id_sequence_obj, custom_label_info, 1)

    output_script = ""
    output_script += ((auxiliary_portrait + '\n')
                      if auxiliary_portrait else "")
    output_script += ((cut_in_statement + '\n') if cut_in_statement else "")
    output_script += ((selling_point + '\n') if selling_point else "")
    output_script += retain_statement if retain_statement else ""

    for title in _STRONG_TITLES:
        output_script = output_script.replace(title, f"<strong>{title}</strong>")
    return product_name, output_script


__all__ = ["build_legacy_consts", "legacy_output", "cut_in_str2tuple"]
