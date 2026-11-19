# -*- coding: utf-8 -*-
"""维表原始列 → 业务侧 dict 的拆解加工（只在读取侧执行，不写回库）。

库里只保存 Excel 的原始单元格文本；本模块负责：
  ``cut_in_str2tuple``          把「切入话术…拒绝挽留话术…」拆成 [切入, 挽留]
  ``build_cut_in_script_dict``  按「匹配结果2」分组、对齐「标签0…标签N」重建嵌套 dict

两个纯函数不依赖任何 I/O，可单独调用、单独测试。仅依赖 ``re``。

两条分组规则：
  - 「匹配结果2」为空时整条置为 ``{}``（含兜底话术一并丢弃）
  - 分组数超过标签列数量时，多出来的分组填 ``""``
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .models import CutInScriptSpec
from .protocols import Row


def cut_in_str2tuple(fusion_scripts: str):
    """把话术文本拆成 [切入话术, 挽留话术]；两段都为空时返回 ""。"""
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


def _text(value):
    return "" if value is None else str(value)


def build_cut_in_script_dict(rows: Iterable[dict], key_col: str, fallback_col: str,
                             groups_col: str, tag_cols: Sequence[str],
                             fallback_key: str = "兜底话术") -> dict:
    """由原始行重建 ``{business_id: {标签ID / 兜底话术: [切入, 挽留] 或 ""}}``。

    :param rows: 可迭代的「列名 → 值」映射，即仓储查出的原始行
    :param tag_cols: 有序标签列名，下标 g 对应 Excel「标签{g}」列
    """
    result = {}
    for row in rows:
        key_id = _text(row[key_col])

        fallback = _text(row[fallback_col])
        inner = {fallback_key: cut_in_str2tuple(fallback.strip().replace("**", ""))}

        groups = _text(row[groups_col])
        if groups.strip():
            for g, one_id in enumerate(groups.strip().split('\n')):
                if g < len(tag_cols):
                    tag_text = _text(row[tag_cols[g]])
                    if tag_text.strip():
                        inner[one_id] = cut_in_str2tuple(tag_text.strip().replace("**", ""))
                        continue
                inner[one_id] = ""
            result[key_id] = inner
        else:
            # 无分组 ID 时整条置空 dict
            result[key_id] = {}

    return result


class CutInScriptTransformer:
    """把 ``CutInScriptSpec`` 的列角色收拢成一次「查列 → 重建」的调用。

    持有不可变配置，无运行状态；``select_columns`` 交给仓储查询，``build()``
    把查回的原始行交回纯函数。这样 ``DimensionReader`` 不必逐个传列名参数。
    """

    def __init__(self, spec: CutInScriptSpec) -> None:
        self._spec = spec

    @property
    def select_columns(self) -> tuple[str, ...]:
        return self._spec.select_columns

    def build(self, rows: Iterable[Row]) -> dict:
        spec = self._spec
        dicts = (dict(zip(self.select_columns, row)) for row in rows)
        return build_cut_in_script_dict(
            dicts,
            key_col=spec.key, fallback_col=spec.fallback, groups_col=spec.groups,
            tag_cols=spec.tags, fallback_key=spec.fallback_key,
        )


__all__ = ["cut_in_str2tuple", "build_cut_in_script_dict", "CutInScriptTransformer"]
