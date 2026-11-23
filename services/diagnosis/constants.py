"""诊断的 AVL 前缀与业务口径常量（纯静态、零 I/O）。"""
from __future__ import annotations

import re

AVL_SCENE_RANK = "F09627"          # DIM_MARKET_SCENE_RANK_ONCE 场景优先级
AVL_PRODUCT_SCENE = "F09628"       # DIM_MARKET_PRODUCT_SCENE_ONCE 产品-场景
AVL_PRODUCT_BUSINESS = "F09629"    # DIM_MARKET_PRODUCT_BUSINESS_ONCE 产品策略
AVL_SCENE_QRHUASHU = "F09630"      # DIM_MARKET_SCENE_QRHUASHU_ONCE 场景-切入话术
AVL_PRODUCT_YXHUASHU = "F09631"    # DIM_MARKET_PRODUCT_YXHUASHU_ONCE 产品-营销话术
AVL_PRODUCT_WLHUASHU = "F09632"    # DIM_MARKET_PRODUCT_WLHUASHU_ONCE 产品-挽留话术
AVL_CONF_USER_SCENE = "F09633"     # DWD_MARKET_CONF_USER_SCENE_DS 用户场景
AVL_TAGID_TAGNAME = "F09635"       # TAGID_TAGNAME 标签ID-标签名（第 8 张表）

# 多值分隔符：竖线 / 全角竖线 / 单元格内换行；确定正式分隔符后只改此处。
MULTI_VALUE_SPLIT_RE = re.compile(r"[|｜\n]+")

# 用户口径：从 PRODUCT_BUSINESS 按 product_id 取 BUSINESS_NAME，按括号拆：
# 【】内 → 资费名称，【】外 → 资费摘要。支持对照表里写的 ASCII [] 写法。
# 样例：『【五大重点客群】不限量客户10元享40G阈值扩容』
#       → 名称「五大重点客群」，摘要「不限量客户10元享40G阈值扩容」
# 无括号时（如「日照中高端临界用户」）名称为空、摘要为全文。
FEE_BRACKET_RE = re.compile(r"【([^】]*)】|\[([^\[\]]*)\]")

# 三类话术文本里的标签占位符：# 开头、& 结尾，中间是 F09635 的「标签名」。
# 例："流量超出后#客运个性化运营标签34&收费较高" → 用「客运个性化运营标签34」
# 查 F09635 得 tag_id（= IOP 的 ITEMCODE），再用 ITEMCODE 对应的 ITEMVALUE 替换。
# 中间不允许再出现 # / &，避免跨占位符贪婪匹配。
TAG_PLACEHOLDER_RE = re.compile(r"#([^#&]+)&")

# RN 字段名是「场景优先级排序」，按 rank 语义取小值优先（RN 越小优先级越高）。
# 口径由该常量控制。
SMALLER_RN_HIGHER_PRIORITY = True

# 严格口径：交集场景关联 F09627 选出 RN 最高者，再按该场景取 F09630。
# 生产维表可能未全量对齐（交集场景在优先级表里没有记录），开启后允许
# 无 RN 的交集场景按原顺序补位，取第一个能查到切入话术的场景。
ALLOW_ENTRY_SCRIPT_FALLBACK = True

EMPTY_TEXT = "无"                  # 空值占位
