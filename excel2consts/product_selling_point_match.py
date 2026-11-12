import sys
from pathlib import Path

project_path = str(Path(__file__).resolve().parent.parent)
sys.path.append(project_path)

import re
import pandas as pd
from yapf.yapflib.yapf_api import FormatFile
from excel2consts.basic_ops import remove_old_node, add_new_dict_node, add_new_nested_dict_node


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
                split_statement[0] = '<strong><span style="color: blue;">' + split_statement[
                    0] + "：" + '</span></strong>'
                statement_list.append(split_statement[0] + '：'.join(split_statement[1:]))

        return '\n'.join(statement_list)

    injection_scripts = []
    pattern = re.compile(r'切入话术(.*?)拒绝挽留话术(.*)', re.DOTALL)

    extract = pattern.findall(fusion_scripts)
    if extract and len(extract[0]) == 2:
        cut_in_statement = remake_statement(extract[0][0].strip())
        cut_in_statement = "【切入话术】\n" + cut_in_statement if cut_in_statement else ""
        injection_scripts.append(cut_in_statement)
        retention_statement = remake_statement(extract[0][1].strip())
        retention_statement = "【挽留话术】\n" + retention_statement if retention_statement else ""
        injection_scripts.append(retention_statement)
    else:
        injection_scripts.append('')

    # 如果全部切入挽留话术都解析失败，则返回空字符串
    for one_content in injection_scripts:
        if one_content:
            break
    else:
        injection_scripts = ""

    return injection_scripts


def rewrite_product_selling_point_match_dict(excel_path, file_path, *node_dict_names):
    # 读取Excel文件
    df = pd.read_excel(excel_path, dtype=str, keep_default_na=False)

    # 改写 product_selling_point_match_dict
    keys = list(df["业务ID"])
    product_names = list(df["产品名称"])
    selling_points = list(df["生成话术"])  # 对应“卖点”输出
    label_ids = list(df["标签ID"])  # 对应“客户画像”输出

    keys = [keyword.strip() if keyword else f"line{i + 2}" for i, keyword in enumerate(keys)]
    product_name = [product_name.strip() if product_name else "" for product_name in product_names]
    values = [selling_point.strip() if selling_point else "" for i, selling_point in enumerate(selling_points)]
    label_id = [label_id.strip() if label_id else "" for label_id in label_ids]

    new_values = zip(product_name, values, label_id)

    my_dict = dict(zip(keys, new_values))
    if '' in my_dict:
        my_dict.pop('')

    # 删除旧的查找字典
    remove_old_node(file_path, node_dict_names[0])

    # 添加新的字典
    add_new_dict_node(file_path, node_dict_names[0], my_dict)

    # 改写 cut_in_script_dict
    nested_dict = {}
    for i, (index, row) in enumerate(df.iterrows()):
        key_id = row["业务ID"]
        script_combo = cut_in_str2tuple(row[f"兜底话术"].strip().replace("**", ""))
        inner_dict = {"兜底话术": script_combo}
        if "匹配结果2" in row.keys() and row["匹配结果2"].strip():
            id_group_list = row["匹配结果2"].strip().split('\n')
            for g, one_id in enumerate(id_group_list):
                if f"标签{g}" in row.keys() and row[f"标签{g}"].strip():
                    script_combo = cut_in_str2tuple(row[f"标签{g}"].strip().replace("**", ""))
                    inner_dict[one_id] = script_combo
                else:
                    inner_dict[one_id] = ""

            nested_dict[key_id] = inner_dict
        else:
            nested_dict[key_id] = {}

    # 删除旧的查找字典
    remove_old_node(file_path, node_dict_names[1])

    # 添加新的字典
    add_new_nested_dict_node(file_path, node_dict_names[1], nested_dict)

    # 整理格式
    FormatFile(str(file_path), in_place=True)


if __name__ == '__main__':
    excel_path_ = Path(project_path) / 'excel2consts' / '卖点维表.xlsx'
    file_path_ = Path(project_path) / 'src' / 'consts.py'
    rewrite_product_selling_point_match_dict(excel_path_, file_path_,
                                             "product_selling_point_match_dict", "cut_in_script_dict")
