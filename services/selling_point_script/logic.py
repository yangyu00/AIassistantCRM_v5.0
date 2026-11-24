"""卖点维表话术：话术输出内容格式。

数据走现有 db 链路：

* ``services.dimensions.SellingPointRow`` 一行（``local_excel2db`` 把
  ``卖点维表.xlsx`` 转成 ``dimensions.db`` 后的只读结果）提供产品名称 /
  生成话术 / 标签ID / 兜底话术 / 匹配结果2 / 标签0…标签N 原文；
* ``custom_label_info`` 是请求级 IOP 返回 ``{ITEMCODE: [ITEMNAME, ITEMVALUE]}``，
  既用来拼【辅助画像】，也用来决定命中哪一组切入 /
  挽留话术；
* ``label_name_mapping`` 来自 db 里的 ``label_name_mapping`` 表（写入侧由
  ``标签转换维表.xlsx`` 产出）。

本模块纯函数、零 I/O、零第三方依赖，不 import 诊断包、不查诊断 8 表、不做
``#标签名&`` 替换。输出顺序 / HTML / 字面换行固定如下：

    辅助画像 → 切入话术 → 卖点 → 挽留话术

四个【】标题统一包 ``<strong>``；话术行前缀用 ``color: blue;``（带空格），
卖点首段内容用 ``color:red;``；段落之间用字面 ``\\n``，**不**转成 ``<br>``。
「匹配结果2」为空时整张话术表为空（例外见下）；
「兜底话术」列缺失 / 为空时回退「标签0」原文充当兜底话术组，其余「标签N」
仍只服务「匹配结果2」，不参与兜底。

之后要改「从维表里抽哪些信息」只改 ``extract_selling_point_fields()``，
要改「抽出来的信息怎么组合」只改 ``build_output_script()``。
"""
from __future__ import annotations

import re

# 四段顺序与标题。
SELLING_POINT_SECTIONS = ("辅助画像", "切入话术", "卖点", "挽留话术")
STRONG_TITLES = tuple(f"【{s}】" for s in SELLING_POINT_SECTIONS)

# 硬编码标签特判。
# 这些 ID 是 IOP 的 ITEMCODE，与 get_customer_labels() 返回的 key 同一口径。
_LABEL_ZERO_TO_WU = "a1e357f57b474d51119bf003443bd088"
_LABEL_FEE_YUAN = "cb933e7eb32b67ec637c78c6f2be65a1"
_LABEL_FLOW_M = ("44aad5383d8d8910e72bd8f74f1cd1df",
                 "bbc3b62f605a0112d7bde076eb24fea5")
_LABEL_BOOL = "1c7b8fdc55e00c4300adcf1c0543f00f"
_LABEL_DOWNGRADE_RED = ("0e6f7d4a8b32aa551d09174481857c39",
                        "efe5295c6763e2eab16afbaa984a9ee4")

_FUSION_RE = re.compile(r'切入话术(.*?)拒绝挽留话术(.*)', re.DOTALL)
_NUMBER_PREFIX_RE = re.compile(r'^\d+\s?\.\s?')
_SELLING_POINT_HEAD_RE = re.compile(r'\d、.*?：', re.DOTALL)


def _warn(msg):
    print(f"WARNING:: {msg}", flush=True)


def strong_sections(text):
    """四个【】标题统一包 ``<strong>``。"""
    for title in STRONG_TITLES:
        text = text.replace(title, f"<strong>{title}</strong>")
    return str(text)


# ---------------------------------------------------------------------------
# ① 话术单元格 → [切入段, 挽留段]
# ---------------------------------------------------------------------------
def parse_fusion_scripts(text):
    """解析一个话术单元格 → ``[切入话术, 挽留话术]``；全解析失败返回 ``""``。

    标题按 ``切入话术`` / ``拒绝挽留话术`` 切两段；段内每一行按首个全角冒号
    拆成「蓝色加粗前缀 + 正文」，半角冒号先统一成全角，正文里剩余的冒号用
    ``'：'.join`` 原样拼回。``**`` 加粗标记由调用方在传入前去掉。
    """
    def remake_statement(extract_chunk):
        statement_list = []
        for one_cut_in_statement in extract_chunk.splitlines():
            if not one_cut_in_statement:
                continue
            split_statement = one_cut_in_statement.strip().replace(':', '：').split("：")
            if len(split_statement) == 1:
                statement_list.append(split_statement[0])
            else:
                split_statement[0] = _NUMBER_PREFIX_RE.sub('', split_statement[0])
                split_statement[0] = ('<strong><span style="color: blue;">'
                                      + split_statement[0] + "："
                                      + '</span></strong>')
                statement_list.append(
                    split_statement[0] + '：'.join(split_statement[1:]))
        return '\n'.join(statement_list)

    injection_scripts = []
    extract = _FUSION_RE.findall(str(text or ""))
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

    # 全部切入挽留话术都解析失败 → 返回空字符串
    for one_content in injection_scripts:
        if one_content:
            break
    else:
        injection_scripts = ""
    return injection_scripts


cut_in_str2tuple = parse_fusion_scripts


def _clean_script_text(text):
    """调用处口径：``.strip().replace("**", "")``。"""
    return str(text or "").strip().replace("**", "")


def _fallback_source(row):
    """兜底话术来源：优先「兜底话术」列；列缺失 / 为空时回退「标签0」。

    返回 ``(原文, 是否回退到标签0)``。只回退「标签0」，不回退其它「标签N」：
    「标签N」只服务「匹配结果2」的场景 ID 映射。
    """
    fallback = str(getattr(row, "fallback_script", "") or "").strip()
    if fallback:
        return fallback, False
    tag_texts = getattr(row, "tag_texts", ()) or ()
    tag0 = str(tag_texts[0] or "").strip() if tag_texts else ""
    return tag0, bool(tag0)


def pick_script_text(row):
    """「兜底话术」列原文；列缺失 / 为空时回退「标签0」原文。"""
    return _fallback_source(row)[0]


def build_script_dict(row):
    """维表一行 → 按「匹配结果2」构造的话术字典。

    「兜底话术」列缺失 / 为空时，用「标签0」原文充当兜底话术组；此时即使
    「匹配结果2」为空，兜底组也仍然生效（否则 tag0 会被整张空话术表吞掉）。
    其余情况维持原口径：「匹配结果2」为空 → ``{}``，切入 / 挽留两段不输出。
    """
    fallback_text, fallback_uses_tag0 = _fallback_source(row)
    fallback_group = parse_fusion_scripts(_clean_script_text(fallback_text))
    match_result = str(getattr(row, "match_result", "") or "").strip()
    if not match_result:
        return {"兜底话术": fallback_group} if fallback_uses_tag0 else {}

    inner_dict = {"兜底话术": fallback_group}
    tag_texts = getattr(row, "tag_texts", ()) or ()
    for g, one_id in enumerate(match_result.split('\n')):
        raw = str(tag_texts[g]).strip() if g < len(tag_texts) else ""
        if raw:
            inner_dict[one_id] = parse_fusion_scripts(_clean_script_text(raw))
        else:
            inner_dict[one_id] = ""
    return inner_dict


def _item_value(item):
    """取 IOP 一项的 ITEMVALUE，兼容 ``[ITEMNAME, ITEMVALUE]`` 与 dict。"""
    if isinstance(item, dict):
        return item.get("ITEMVALUE")
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return item[1]
    return None


def _label_nv(item):
    """取 ``(ITEMNAME, ITEMVALUE)``；异常形状按缺失处理。"""
    if isinstance(item, dict):
        return (item.get("ITEMNAME"), item.get("ITEMVALUE"))
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return (item[0], item[1])
    return None


def select_script(script_dict, custom_label_info, index):
    """按 IOP 命中情况选切入（index=0）/ 挽留（index=1）段。

    按 dict 插入顺序找第一个「ID 在 IOP 返回里且 ITEMVALUE ∈ {是, 1}」的组，
    全不命中才用「兜底话术」组；异常一律降级空串，不抛给在线接口。
    """
    fallback_script = ""
    try:
        for search_id, fusion_script_list in script_dict.items():
            if (search_id != "兜底话术"
                    and search_id in custom_label_info.keys()
                    and _item_value(custom_label_info[search_id]) in ("是", "1")
                    and fusion_script_list[index]):
                return fusion_script_list[index]
        else:
            if "兜底话术" in script_dict.keys():
                fusion_script_list = script_dict["兜底话术"]
                if isinstance(fusion_script_list, list):
                    fallback_script = fusion_script_list[index]
    except IndexError:
        # 「匹配结果2」里解析失败的话术组存的是 ""，取 [0]/[1] 必然抛
        # IndexError；try/except 直接吞掉并 return ""，
        # 不打日志（真实维表里有大量这类行，刷屏无意义）。
        return ""
    except Exception as e:
        _warn(f"切入挽留话术读取错误：{e}")
        return ""
    return fallback_script


# ---------------------------------------------------------------------------
# ② 生成话术 → 【卖点】段
# ---------------------------------------------------------------------------
def build_selling_point_section(old_text):
    """把「生成话术」列按 ``1、资费：/ 2、价格：/ 3、业务确认：`` 分段。

    首段标题加粗、首段内容标红加粗；后续段标题单独加粗、内容原样。
    """
    old_text = str(old_text or "")
    extract = _SELLING_POINT_HEAD_RE.findall(old_text)
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
    split_chars_list = list(extract[1:])
    split_rule = "|".join(re.escape(char) for char in split_chars_list)
    for i, g in enumerate(re.split(split_rule, ready_join_text)[1:]):
        fusion_text += ("\n" + '<strong>' + split_chars_list[i]
                        + '</strong>' + g.strip())
    return fusion_text


build_selling_point = build_selling_point_section


# ---------------------------------------------------------------------------
# ③ 标签ID + IOP 返回 → 【辅助画像】
# ---------------------------------------------------------------------------
def build_auxiliary_portrait(label_ids, custom_label_info,
                             label_name_mapping=None):
    """按维表「标签ID」子集对齐 IOP 名称与值，逐项拼辅助画像。

    名称可经 ``label_name_mapping`` 换成营销助手显示名；值按上一版本的硬编码
    规则做「0.00→无 / 费用加元 / 流量加M / 是否→是否 / 降档阈值标红 / 同名去重」
    转换。转换只影响文本，不改变 HTML 结构。
    """
    mapping = label_name_mapping or {}
    labels = custom_label_info or {}
    queried_label_ids = list(label_ids or ())

    portrait_str = ""
    exist_label_name_list = []
    try:
        for label_id in queried_label_ids:
            label_nv = _label_nv(labels.get(label_id))
            if label_nv is None:
                continue
            label_name, label_value = label_nv[0], label_nv[1]
            if not label_value:
                continue

            if label_id in mapping.keys():
                label_name = mapping[label_id]

            # 近期上网掉线、卡顿记录的特殊处理
            if label_id == _LABEL_ZERO_TO_WU and str(label_value) == "0.00":
                label_value = "无"

            # 涉及费用的标签转换规则
            if label_id == _LABEL_FEE_YUAN:
                label_value = ("0元" if str(label_value) == "0.00"
                               else str(label_value) + "元")

            # 抖音快手使用流量后加「M」单位
            if label_id in _LABEL_FLOW_M:
                label_value = str(label_value) + "M"

            # 1,0 切换成 是, 否
            if "是否" in label_name and str(label_value) == "1":
                label_value = "是"
            elif "是否" in label_name and str(label_value) == "0":
                label_value = "否"
            if label_id == _LABEL_BOOL:
                label_value = ("是" if str(label_value) == "1" else "否")

            # 涉及是否的重复标签去重（replace 结果未回写）
            if label_name in exist_label_name_list:
                if label_value == "是":
                    portrait_str.replace(f"{label_name}: 否; ",
                                         f"{label_name}: 是; ")
                    continue
                continue

            # 降档阈值的特殊处理：名称标红加粗，值不加粗
            if label_id in _LABEL_DOWNGRADE_RED:
                portrait_str += (f'<strong><span style="color:red;">'
                                 f'{label_name}</span></strong>: {label_value}; ')
                continue

            portrait_str += (f'{label_name}: <strong><span style="color:blue;">'
                             f'{label_value}</span></strong>; ')
            exist_label_name_list.append(label_name)
    except Exception as e:
        # IOP 返回异常形状（如 ITEMNAME 为 None）时降级为空画像，不抛 500。
        _warn(f"辅助画像拼装失败，降级为空：{e}")
        return ""

    if portrait_str:
        portrait_str = "【辅助画像】" + portrait_str
        portrait_str = portrait_str.rstrip('; ')
    return portrait_str


# ---------------------------------------------------------------------------
# ④ 组合出口
# ---------------------------------------------------------------------------
_OUTPUT_KEYS = ("auxiliary_portrait", "cut_in_statement",
                "selling_point_block", "retain_statement")


def build_output_script(record):
    """**唯一组合点**：各段按固定顺序拼成 ``output_script``。

    字面 ``\\n`` 连接，最后把四个【】标题统一包 ``<strong>``；不做
    ``strip()``、不把 ``\\n`` 转成 ``<br>``，与上一版本逐字符一致。
    """
    record = record or {}
    out = ""
    for i, key in enumerate(_OUTPUT_KEYS):
        value = record.get(key)
        if value:
            out += value + ("" if i == len(_OUTPUT_KEYS) - 1 else "\n")
    return strong_sections(out)


def extract_selling_point_fields(product_id, row, custom_label_info=None,
                                 label_name_mapping=None):
    """**唯一抽取点**：db 一行 + IOP 返回 → 各段字段 dict。

    维表无该行、或该行「产品名称」为空 → ``None``（工厂据此落 ``code: "2"``）。
    其余字段全部来自 db 原文与 IOP 返回，不读诊断 8 表、不做标签占位符替换。
    """
    if row is None:
        return None
    product_name = str(getattr(row, "product_name", "") or "").strip()
    if not product_name:
        return None

    labels = custom_label_info or {}
    label_ids = list(getattr(row, "label_ids", ()) or ())
    script_dict = build_script_dict(row)

    return {
        "product_id": product_id,
        "business_name": product_name,
        "queried_label_ids": label_ids,
        "auxiliary_portrait": build_auxiliary_portrait(
            label_ids, labels, label_name_mapping),
        "cut_in_statement": select_script(script_dict, labels, 0),
        "selling_point_block": build_selling_point_section(
            getattr(row, "selling_point", "")),
        "retain_statement": select_script(script_dict, labels, 1),
    }


__all__ = [
    "SELLING_POINT_SECTIONS", "STRONG_TITLES", "strong_sections",
    "parse_fusion_scripts", "cut_in_str2tuple", "pick_script_text",
    "build_script_dict", "select_script",
    "build_selling_point_section", "build_selling_point",
    "build_auxiliary_portrait", "build_output_script",
    "extract_selling_point_fields",
]
