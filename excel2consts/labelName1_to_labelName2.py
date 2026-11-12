import sys
from pathlib import Path
project_path = str(Path(__file__).resolve().parent.parent)
sys.path.append(project_path)

import pandas as pd
from yapf.yapflib.yapf_api import FormatFile
from excel2consts.basic_ops import remove_old_node, add_new_dict_node
from collections import Counter


def find_duplicates(lst):
    counter = Counter(lst)
    duplicates = [k for k, v in counter.items() if v > 1]
    return duplicates


def rewrite_label_name_mapping(excel_path, node_dict_name, file_path):
    # 读取Excel文件
    df = pd.read_excel(excel_path, dtype=str, keep_default_na=False)

    keys = list(df["标签ID"])
    values = list(df["营销助手显示名字"])

    keys = [keyword.strip() if keyword else f"line{i + 2}" for i, keyword in enumerate(keys)]
    values = [value.strip() if value else f"line{i + 2}" for i, value in enumerate(values)]
    my_dict = dict(zip(keys, values))
    if '' in my_dict:
        my_dict.pop('')

    # 删除旧的查找字典
    remove_old_node(file_path, node_dict_name)

    # 添加新的字典
    add_new_dict_node(file_path, node_dict_name, my_dict)

    FormatFile(str(file_path), in_place=True)


if __name__ == '__main__':
    excel_path_ = Path(project_path) / 'excel2consts' / '标签转换维表.xlsx'
    file_path_ = Path(project_path) / 'src' / 'consts.py'
    rewrite_label_name_mapping(excel_path_, "label_name_mapping", file_path_)
