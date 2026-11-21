# -*- coding: utf-8 -*-
"""诊断新线的 AVL 前缀与业务口径常量（纯静态、零 I/O）。

字段口径来自《营销话术字段取数逻辑说明_中英对照版.pdf》与
《话术涉及表结构.xls》；表注册见 ``sftp_avl2db/table_schemas.py``。
不确定的两处口径（多值分隔符、优先级方向）集中在这里，便于上游确认后只改一处。
"""
from __future__ import annotations

import re

# ---- 诊断新线 8 个 AVL 前缀（F09635 为预留，主链路不消费） ----
AVL_SCENE_RANK = "F09627"          # DIM_MARKET_SCENE_RANK_ONCE 场景优先级
AVL_PRODUCT_SCENE = "F09628"       # DIM_MARKET_PRODUCT_SCENE_ONCE 产品-场景
AVL_PRODUCT_BUSINESS = "F09629"    # DIM_MARKET_PRODUCT_BUSINESS_ONCE 产品策略
AVL_SCENE_QRHUASHU = "F09630"      # DIM_MARKET_SCENE_QRHUASHU_ONCE 场景-切入话术
AVL_PRODUCT_YXHUASHU = "F09631"    # DIM_MARKET_PRODUCT_YXHUASHU_ONCE 产品-营销话术
AVL_PRODUCT_WLHUASHU = "F09632"    # DIM_MARKET_PRODUCT_WLHUASHU_ONCE 产品-挽留话术
AVL_CONF_USER_SCENE = "F09633"     # DWD_MARKET_CONF_USER_SCENE_DS 用户场景
AVL_TAGID_TAGNAME = "F09635"       # 标签ID-标签名（取数说明未涉及，预留）

# ---- 多值单元格拆分 ----
# 取数说明 04 / 08 只写「按指定分隔符」未给出具体字符；样例单元格里未出现多值。
# 默认支持半角/全角竖线与单元格内换行（与仓库既有 .split("\n") 的维表约定一致）。
# 话术文本里几乎不会用竖线，可避免把中文逗号/分号误当成多值边界。
# 上游给出正式分隔符后只改这一处正则。
MULTI_VALUE_SPLIT_RE = re.compile(r"[|｜\n]+")

# ---- 资费名称 / 资费摘要的括号拆分 ----
# 用户口径：从 PRODUCT_BUSINESS 按 product_id 取 BUSINESS_NAME，按括号拆：
# 【】内 → 资费名称，【】外 → 资费摘要。兼容对照表里写的 ASCII [] 写法。
# 样例：『【五大重点客群】不限量客户10元享40G阈值扩容』
#       → 名称「五大重点客群」，摘要「不限量客户10元享40G阈值扩容」
# 无括号时（如「日照中高端临界用户」）名称为空、摘要为全文。
FEE_BRACKET_RE = re.compile(r"【([^】]*)】|\[([^\[\]]*)\]")

# ---- 场景优先级方向 ----
# RN 字段名是「场景优先级排序」，按 rank 语义取小值优先（RN 越小优先级越高）。
# 若业务口径相反，改为 False 即可，其余代码不动。
SMALLER_RN_HIGHER_PRIORITY = True

# ---- 切入话术的降级开关 ----
# 严格口径：交集场景关联 F09627 选出 RN 最高者，再按该场景取 F09630。
# 生产维表可能未全量对齐（交集场景在优先级表里没有记录），开启后允许
# 无 RN 的交集场景按原顺序补位，取第一个能查到切入话术的场景。
ALLOW_ENTRY_SCRIPT_FALLBACK = True

EMPTY_TEXT = "无"                  # 与现有 rewrite 模块的空值占位保持一致
