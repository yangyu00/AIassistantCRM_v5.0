# services · 在线话术业务层

`services/` 是在线 API 的**业务层**：从业务库取数、按 `product_id` 选业务、
把原始记录重组成话术对象。`main.py` 只保留 HTTP 编排（入参校验、IOP 标签
提供者、请求级画像缓存、响应组装），不再持有路由表，也不直接查库。

> **整合说明（2026-11-21）**：根目录 `诊断新线_在线链路实现说明.md` 的全文
> 已并入本文件「诊断新线 · 在线链路实现说明」及各相关章节，本文件即在线链路
> 的唯一说明入口；原独立文档仍保留在仓库根目录作归档，可按需删除或改为指向
> 本文件的存根。原文档中指向包外的相对链接已按 `services/` 视角修正。

> **职责边界（写入侧 / 在线侧彻底解耦）**
> - `sftp_avl2db/`：**纯写入侧**，SFTP AVL → SQLite，产出业务库；不 import 本层，也不声明在线侧的库路径、表字段或查询条件。
> - `local_excel2db/`：**卖点维表纯写入侧**，产出 `dimensions.db`；不 import 本层，也不提供查询入口。
> - `services/db_config.py`：**在线侧唯一的读取配置** —— 各 db 文件路径、表字段（含带类型的冗余声明）、查询键 / 排序、分表数、卖点维表列，全部自己维护；不 import 任何写入侧包。
> - `services/reader.py` / `services/dimensions.py`：两个只读模块，只从 `db_config` 取配置。
>
> 两侧唯一的交集是**磁盘上的 db 文件**（数据契约），没有任何代码 / 配置模块互相引用。表字段在两侧各写一份是刻意接受的冗余：读在线侧代码时不用跳到别的包就能看清 `SELECT` 回来的列顺序。改列顺序 / 分表数 / 库路径时两侧声明必须同步，见 [解耦改造说明](../解耦改造说明.md)。
>
> 全局架构、进程数、跨模块降级总表见[根 README](../README.md)；
> 诊断新线的完整实现说明（字段口径 / 取数路径 / 兜底 / 验证 / 部署）已整合进本文件
> 「诊断新线 · 在线链路实现说明」章节。

## 目录分层

| 层 | 文件 | 职责 |
|---|---|---|
| 包根 | `__init__.py` | 公共 API，`__getattr__` 延迟导出；**`import services` 零 I/O** |
| **读取配置** | **`db_config.py`** | **在线侧唯一配置来源**：`AVL_DB_SPECS`（12 个 AVL 各对应哪个 db 文件、查询键、排序、分表）、`TABLE_SCHEMAS`（列顺序与类型）、`SELLING_POINTS_*`（卖点维表列）、`shard_index()` / `table_name()`、`load()` / `validate()` |
| 配置模板 | `.env.example` | `AVL_DB_ROOT` / `DIMENSIONS_DB` / 两个 `NUM_TABLES`；复制成 `.env` 后生效，缺文件用代码内默认值 |
| 公共基类 / 画像组件 | `product_info_raw.py` | `ProductInfoBase`（最小公共形状）、`AuxiliaryPortrait`（商机专属，组合）、`ProductInfoRaw`（第 4 类兜底对象）、`build_auxiliary_portrait()` |
| 业务库读取侧 | `reader.py` | `query` / `query_many` / `query_in`；只认 AVL 前缀 |
| 卖点维表读取侧 | `dimensions.py` | `load_selling_points()` / `split_label_ids()`；`product_id → label_ids / product_name / match_result / tag_*` |
| 路由 | `routing.py` | `product_id → AVL` 两张静态表、`need_labels()`、`build_product_object()` |
| 商机回捞 | `opportunity_recall/` | 四段话术（通话总结 / 用户需求 / 已推荐政策 / 拒绝原因）+ 辅助画像 |
| 控损 | `loss_control/` | `strategy_name` 自定义标签 → HTML |
| 诊断新线 | `diagnosis/` | 取数、纯逻辑、六段话术对象、口径常量、**卖点维表兜底** |

三类业务包一律以**业务名**命名（与 `diagnosis/` 对齐），包内 `rewrite.py`
承载业务对象、`__init__.py` 显式导出；不保留按 spec 编号的历史目录名，也不
保留 `ProductInfoRewrite` 一类旧类名别名。控损的标签解析抽成了模块级纯函数
`parse_custom_tags()`，可单独单测。

**对象模型**：四类对象只继承极简的 `ProductInfoBase`（`product_id` /
`product_name` / `output_script` + 统一日志头）。辅助画像用组合挂在商机对象
的 `AuxiliaryPortrait` 组件上；IOP 标签与卖点维表行映射只存在于商机，画像
结果只缓存在该组件实例上，控损 / 诊断 / 兜底对象连这些属性都没有。旧的
`product_selling_point_match_dict` 已改名为 `selling_point_rows`，且不再由
基类持有。`build_product_object()` 只收标签与维表行两项数据，按分支只转发
各业务真正消费的参数；三类子类不再各写一遍公共日志行，改由基类的
`_extra_str_lines()` 钩子统一拼装。

| 业务 | 对外 API | 定义位置 |
|---|---|---|
| 商机回捞 | `ProductInfoByOpportunityRecall` | `opportunity_recall/rewrite.py` |
| 控损 | `ProductInfoByLossControl` | `loss_control/rewrite.py` |
| 诊断新线 | `ProductInfoByDiagnosis` | `diagnosis/rewrite.py` |

## 四类路由（顺序不可调换）

`build_product_object(product_id, telnum, *, custom_label_info, selling_points)`
是**唯一对象工厂**，返回一个产品对象；`main.py` 用
`isinstance(obj, BUSINESS_OBJECT_TYPES)` 判 `code`（`"0"` 带话术 / `"2"` 空话术）。

| 顺序 | 条件 | 数据源 | 对象 |
|---|---|---|---|
| 1 | 商机回捞 3 个 ID | `reader.query(avl, telnum)` | `ProductInfoByOpportunityRecall` |
| 2 | 控损 1 个 ID，**有记录才命中** | `reader.query(avl, telnum)` | `ProductInfoByLossControl` |
| 3 | 诊断新线 | `diagnosis.build_diagnosis_record()`（7 表关联）；任一环节失败改用卖点维表兜底 | `ProductInfoByDiagnosis` |
| 4 | 其余 | — | `ProductInfoRaw`（兜底） |

控损分支未查到记录时**不短路**，继续往下走诊断与兜底（与重构前 `main.py`
一致）。诊断新线不维护静态映射：`F09629`（`DIM_MARKET_PRODUCT_BUSINESS_ONCE`）
本身就是「产品ID → 策略 / 产品」映射表。**7 表关联任一环节失败时用卖点维表
兜底，仍返回 `ProductInfoByDiagnosis`（`code: "0"`）**；只有连维表里也没有该
`product_id` 时才落第 4 类 `code: "2"`，详见「诊断新线 → 卖点维表兜底」。

`product_id → AVL 前缀` 的两张静态表在 `routing.py`，是**唯一维护点**；
`reader` 完全不知道产品 ID，只按 `db_config` 声明的 `{project}/{AVL}.db` 定位。
`selling_points` 由 `main.py` 在循环外调一次 `dimensions.load_selling_points()`
批量查回，只透传给对象工厂，不参与路由判定。

输出段按**对象类型**判定 `code`，不靠 `product_name` 是否为空——兜底对象也可能
因维表残留拿到非空名称，只看它会返回「看起来成功、实际无内容」的响应。

## 业务库读取侧：`services/reader.py` + `services/db_config.py`

读取侧的配置**全部在 `db_config.py` 自己维护**，不引用后台写入侧的任何模块：

- `AVL_DB_SPECS`：12 个 AVL 各自的 `db_file`（如 `diagnosis/F09633.db`）、
  schema 名、单条查询键 `where`、固定 `order_by`、是否分表；
- `TABLE_SCHEMAS` / `TABLE_PRIMARY_KEYS`：各表的列顺序 / 类型（与写入侧建表
  DDL 对齐，**冗余声明**）与主键；`SELECT *` 回来的 dict 就按这个列顺序组装；
- `services/.env`（模板 `.env.example`）：`AVL_DB_ROOT`、`DIMENSIONS_DB`、
  `LOSS_CONTROL_NUM_TABLES`、`F09633_NUM_TABLES`；缺文件时用 `db_config.py`
  顶部的代码内默认值（`sftp_avl2db/runtime/sqlite_db` 等）；
- `shard_index()` / `table_name()`：分表口径 `crc32(product_no) % NUM_TABLES`
  与表名 `{AVL}_{分表号}`，在线侧自己实现。这是物理数据布局，写入侧也按它
  落库；**改分表数时两侧配置必须同步并重灌对应 AVL**。

三个入口都是 `async`、内部同步 `sqlite3`，列名走 `DbProfile.columns` 白名单、
值一律参数绑定，排序固定取静态声明：

| 入口 | 返回 | 语义 |
|---|---|---|
| `query(avl, key)` | `dict` | 单条，`op_time DESC LIMIT 1`；商机 / 控损使用 |
| `query_many(avl, *, where, limit=None)` | `list[dict]` | `where={列名: 标量｜序列}`，标量 `=`、序列 `IN`，多键 AND |
| `query_in(avl, column, values, *, limit=None)` | `list[dict]` | 单列 IN 便捷形式 |

- 库不存在 / 表缺失 / AVL 未注册 / 非法列名 / 查询异常 → 降级 `{}` 或 `[]`，**不抛给在线接口**；空 IN 集合直接短路返回 `[]`。
- 分表 AVL 需**标量** `product_no` 定位分表；缺它或传序列 → 降级 `[]`（不广播到 128 张表、不抛异常）。`query_in` 与不带 `product_no` 的 `query_many` 只对固定表有效，批量号码请按分表键分组后逐个调用。
- `ORDER BY` 固定取 `AvlDbSpec.order_by` 静态声明，**不开放排序注入口**；场景按 `RN` 重排在业务层用 Python 做。
- 模块级 `_CONFIG = db_config.load()` + `db_config.validate(...)` 在**首次 import 本模块时**装配；`db_config` 支持没有 `services/.env` 的干净检出，`import services` 本身不触发。
- 离线打桩：`reader.configure(db_config.load(avl_db_root=临时目录, dimensions_db=临时维表库))`，把查询隔离到临时库，不碰包内 `runtime/`。

多行 / 多条件查询示例：

```python
from services import reader

await reader.query_many("F09628", where={"business_type": bt})           # 多行
await reader.query_in("F09627", "scene", matched_scenes)                  # 批量 IN
await reader.query_many("F09631", where={"business_type": bt,
                                         "yx_huashu": ["话术A"]})          # 多条件 AND
```

### 两个分表 AVL

| AVL | 业务 | 分表键 | 在线查询键 | 分表数（`db_config` 声明） |
|---|---|---|---|---|
| `F09591` | 控损 | `product_no` | `product_no`（手机号） | `LOSS_CONTROL_NUM_TABLES`（默认 128） |
| `F09633` | 诊断·用户场景对应关系 | `product_no` | `product_no`（`telnum`） | `F09633_NUM_TABLES`（默认 128） |

`F09633` 是诊断 8 表里唯一含手机号的表，千万级行；分表键与
`build_diagnosis_record()` 里的 `where={"product_no": telnum}` 完全一致，
所以一次请求**只命中一张分表**，不需要广播。写入侧按同一口径落库，细节见
[`sftp_avl2db/README.md`](../sftp_avl2db/README.md)「F09633 分表」。

## 卖点维表读取侧：`services/dimensions.py`

商机画像的**标签 ID 子集不再硬编码**，诊断失败时的兜底也用同一行维表：
`main.py` 每请求调一次 `load_selling_points(product_ids)`，按本次全部 `productId`
批量 `IN` 查 `selling_points` 表，返回 `{product_id: SellingPointRow}`。

| 项 | 口径（均在 `db_config.py` 声明） |
|---|---|
| 库 | `DIMENSIONS_DB`（默认 `local_excel2db/runtime/sqlite_db/dimensions.db`，指向写入侧产出的文件） |
| 连接 | `sqlite3` + `mode=ro` 只读，**写入侧仍只写、在线侧仍只读** |
| 表 / 列名 | `SELLING_POINTS_TABLE` = `selling_points`；`SELLING_POINTS_SELECT_COLUMNS` = 必需列 + 可选列 + `tag_0 … tag_27`（与 `SELLING_POINTS_SCHEMA` 同源自检） |
| 商机画像用列 | `product_id`（主键）→ `label_id`（Excel「标签ID」） |
| 诊断兜底用列 | `product_name`（产品名称）、`match_result`（「匹配结果2」场景标签 ID）、`tag_0 … tag_27`（「标签0…标签27」话术单元格） |
| 其它列 | `selling_point` / `fallback_script` 保留原文本；可选列缺失填空串 |
| 拆分 | `split_label_ids()`：换行 / 竖线 / 逗号 / 分号（含全角）→ 去空、保序。商机画像**去重**（`unique=True`）；「匹配结果2」**不去重**（`unique=False`），与 `tag_texts` 按下标一一对应 |

`SellingPointRow.scene_label_ids` 是「匹配结果2」按位置拆出的 ID 元组，
`tag_texts` 是 `tag_0 …` 的原文元组，两者**同下标**：第 i 个 ID 对应「标签i」。

- **每请求 1 次、不做进程级缓存**：写入侧按固定间隔整表覆盖，缓存会让新维表不生效；同一请求内靠返回的 dict 复用，重复 ID 只查一次。
- 列名走 `db_config` 白名单、值一律参数绑定，IN 列表按 500 分块。
- 库不存在 / 表缺失 / 必需列漂移 / 查询异常 → 打日志并降级 `{}`；未命中的
  `product_id` 不在返回 dict 中。商机辅助画像为空；诊断也无法兜底（维持
  `code: "2"`），**不抛给在线接口**。
- 离线打桩：`dimensions.configure(临时 dimensions.db)`，传 `None` 还原默认路径；
  注入只改库文件位置，表名与列声明仍是本层静态常量。

> **分工**：商机侧由维表 `label_id` 决定「查哪些 ID」、IOP 返回的
> `custom_label_info` 决定「名称与值」；诊断兜底侧由维表「匹配结果2」决定
> 「按什么顺序找场景 ID」、IOP 返回决定「哪个 ID 命中」，命中后取对应「标签N」
> 的话术。写入侧怎么写库见 [`local_excel2db/README.md`](../local_excel2db/README.md)，
> **在线侧的列映射不取自它**。

## 诊断新线 · 在线链路实现说明

> 原根目录独立文档 `诊断新线_在线链路实现说明.md`（日期 2026-11-21 ｜
> 项目 `yangyu00/AIassistantCRM_v5.0`）已完整整合进本章节。

8 个 AVL（`F09627`～`F09633` / `F09635`）在 `db_config.AVL_DB_SPECS` 里各有
一份在线侧声明；主链路用其中 7 张表，`F09635`（标签ID-标签名）只保留声明、
不进链路。

### 1. 结论与字段口径

`product_id` 命中 `F09629` 即走诊断业务，返回 `code: "0"` 与六段话术；
**7 表关联任一环节拿不到结果时改用卖点维表兜底**，仍返回 `code: "0"`、
四段话术；连维表里也没有该 `product_id` 才走 `code: "2"`。商机回捞 / 控损
两条链路行为不变。

资费名称与资费摘要取 `PRODUCT_BUSINESS`（`F09629`）按 `product_id` 查到的
`BUSINESS_NAME`，按【】拆分——内为资费名称、外为资费摘要。

- `【五大重点客群】不限量客户10元享40G阈值扩容` → 资费名称 `五大重点客群`，资费摘要 `不限量客户10元享40G阈值扩容`
- `日照中高端临界用户`（无括号）→ 资费名称为空，资费摘要为全文
- 兼容中英对照表里写的 ASCII `[]`；多个括号块时名称取第一块
- 响应里的 `productName` 取资费名称，无括号时退到资费摘要

### 2. 六个输出字段的取数路径

输入 `product_id` + `telnum`，输出资费名称 / 资费摘要 / 痛点诊断 / 切入话术 /
营销话术 / 挽留话术。

| 输出字段 | 来源表（AVL） | 关联与规则 |
|---|---|---|
| 资费名称 | `F09629` `DIM_MARKET_PRODUCT_BUSINESS_ONCE` | 按 `product_id` 取 `BUSINESS_NAME`，提取【】内内容 |
| 资费摘要 | 同上 | 同一 `BUSINESS_NAME`，提取【】外内容 |
| 痛点诊断 | `F09633` `DWD_MARKET_CONF_USER_SCENE_DS` + `F09628` `DIM_MARKET_PRODUCT_SCENE_ONCE` | 按 `business_type` 取产品场景列表；按 `telnum` 取用户场景及 `REASON`；取交集，**合并全部交集场景**的 `REASON` |
| 切入话术 | `F09627` `DIM_MARKET_SCENE_RANK_ONCE` → `F09630` `DIM_MARKET_SCENE_QRHUASHU_ONCE` | 交集场景按 `RN` 选**一个**最高优先级场景，再按该 `SCENE` 取 `QR_HUASHU` |
| 营销话术 | `F09631` `DIM_MARKET_PRODUCT_YXHUASHU_ONCE` | 按 `business_type` 取 `YX_HUASHU`，可多行、去重 |
| 挽留话术 | `F09632` `DIM_MARKET_PRODUCT_WLHUASHU_ONCE` | 按 `business_type` 取 `REJECT_REASON` / `WL_HUASHU`，两字段各自拆分后**按相同位置一一配对** |

公共关联路径（取数说明 02）：

```
product_id ──F09629──> BUSINESS_NAME ──【】拆分──> 资费名称 / 资费摘要
                    └> BUSINESS_TYPE ─┬─F09628─> 产品场景列表 ─┐
                                      ├─F09631─> 营销话术       │
                                      └─F09632─> 拒绝原因/挽留   │
telnum ─────F09633──> 用户场景 + REASON（多值按位置配对）────────┴─> 交集场景
交集场景 ───F09627──> 最高 RN 优先级场景 ───F09630─> 切入话术
```

`F09635`（标签ID-标签名）在取数说明里没有位置，不进主链路，仅保留注册。

**`F09633` 是 8 表里唯一带 `product_no` 的表，千万级行，按 `product_no`
分 128 张表**（`F09633_0 … 127`）。分表键与这里的 `telnum` 查询键完全一致，
读取侧按 `crc32(product_no) % NUM_TABLES` 精确定位一张表，不广播；分表数在
写入侧 `sftp_avl2db/.env`（`F09633_NUM_TABLES`）与在线侧 `services/.env`
各自声明，**键名由两侧各自维护，取值必须同步**。在线侧口径见上文「两个分表
AVL」，分表与重灌口径见
[`sftp_avl2db/README.md`](../sftp_avl2db/README.md)「F09633 分表」。

### 3. 实现分层与业务逻辑细则

| 文件 | 职责 |
|---|---|
| `diagnosis/constants.py` | 8 个 AVL 前缀、多值分隔符正则、括号正则、RN 方向、降级开关（待业务确认项集中在这里） |
| `diagnosis/logic.py` | 纯函数零 I/O：括号拆分、多值拆分、按位置配对、交集、优先级排序、HTML 拼装（`build_output_script(..., omit=())` 可整段省略） |
| `diagnosis/query.py` | `build_diagnosis_record(product_id, telnum, *, custom_label_info, selling_points)`，async，7 张表关联 + 兜底触发 |
| `diagnosis/fallback.py` | 卖点维表兜底：`parse_tag_script()` / `has_scene_labels()` / `build_fallback_record()`，纯函数零 I/O |
| `diagnosis/rewrite.py` | `ProductInfoByDiagnosis(ProductInfoBase)`，把 record 拼成 `output_script`；兜底 record 省略两段 |

正常链路输出六段固定：`【资费名称 / 资费摘要 / 痛点诊断 / 切入话术 / 营销话术 / 挽留话术】`，空值填「无」。

- **多值单元格**：取数说明只写「按指定分隔符」未给具体字符，样例里也没有多值。默认支持半角/全角竖线与单元格内换行（话术里几乎不会用竖线，避免误把中文逗号、分号当边界）。`SCENE`/`REASON`、`REJECT_REASON`/`WL_HUASHU` 各自拆分后按相同下标配对；长度不等时短侧补空串，不抛错。
- **场景交集**：按用户场景出现顺序去重，痛点诊断保留**全部**交集场景的 `REASON`（不强行去重）；切入话术只取一个场景。
- **优先级**：`RN` 字段名是「场景优先级排序」，按 rank 语义取 **RN 小者优先**。有 RN 的交集场景排前面，无 RN 的按原顺序补位（可由 `ALLOW_ENTRY_SCRIPT_FALLBACK` 关掉）；最终取第一个能查到 `QR_HUASHU` 的场景，并把 `top_scene_source` 标成 `rank` / `fallback` / `none` 便于排查。
- **降级**：任一查询空 / IOP 无场景标签 / 交集为空 / 六项全空 → 改用卖点维表兜底（见第 5 节），仍 `code: "0"` 四段话术；维表里也没有该 `product_id` 时才返回 `None` → `code: "2"`。
- **幂等与去重**：营销话术多行按内容去重；挽留话术按位置配对后保留全部组合。

**解析器**：`FlatColumnParser` 是唯一的 flat 解析器，商机回捞与诊断 8 张维表
共用：所有列原样保留文本，类型转换交给 SQLite 列亲和性（商机样例 `"3"` 落库
为 integer 3），主键列下标由 `profile.primary_key` 换算，与建表 DDL 同源。
`PARSER_KINDS = ("flat", "continuation")`，8 个诊断 AVL 的 `parser` 均为 `flat`。

### 4. 正常链路输出格式

`output_script` 与现有 rewrite 模块同风格（`<strong>【标签】</strong>` + `<br>`），
六段固定，空值填「无」：

```
<strong>【资费名称】</strong>五福套餐<br>
<strong>【资费摘要】</strong>流量放心用，月费58元<br>
<strong>【痛点诊断】</strong>痛点A<br>痛点B<br>流量超套，近三个月超套2次，合计超套4元<br>
<strong>【切入话术】</strong>您最近流量用的比以前多，流量超出后#客运个性化运营标签34&收费较高，有个活动挺合适。<br>
<strong>【营销话术】</strong>营销话术A<br>营销话术B<br>
<strong>【挽留话术】</strong><strong><span style="color:blue;">价格贵：</span></strong>挽留话术A<br>
<strong><span style="color:blue;">流量够用：</span></strong>挽留话术B
```

`productName` = 资费名称（无括号时退到资费摘要）。

### 5. 卖点维表兜底

**触发**（`diagnosis/query.py` 统一出口 `fallback(reason)`）：

| 条件 | `fallback_reason` |
|---|---|
| 7 个查询任一返回空（或提取出的有效值为空） | `db_empty:{AVL}` |
| IOP 返回里没有场景标签（口径见 `has_scene_labels()`） | `no_scene_labels` |
| 场景交集为空 | `empty_intersection` |
| 六项输出全空 | `empty_output` |

「IOP 返回里没有场景标签」的判定集中在 `fallback.has_scene_labels()`：IOP
返回为空即算没有；维表该行配了「匹配结果2」时，其中至少一个 ID 出现在 IOP
返回里才算有；维表没配「匹配结果2」则退化为「IOP 至少返回了标签」，避免把
没配维表的诊断 ID 全部误判。

**兜底内容**（`diagnosis/fallback.py`，来源都是 `selling_points` 该
`product_id` 行）：

1. `product_name` 按【】拆成资费名称 / 资费摘要 —— **与正常诊断一样**用
   `split_fee_name()`；响应 `productName` 也取资费名称；
2. 「匹配结果2」`match_result` 按位置拆成场景标签 ID（**不去重**），与
   `tag_0 … tag_N`（「标签0…标签N」）一一对应；**顺序遍历**，第一个出现在
   IOP `custom_label_info` 里的即命中并**中断遍历**；
3. 取同下标的「标签N」单元格，`parse_tag_script()` 按 `切入话术` /
   `拒绝挽留话术` 两个标题分段：前者收集非空行用 `<br>` 拼接，后者每行按首个
   全角/半角冒号拆成 `(拒绝原因, 挽留话术)`；解析时去掉 `**` 加粗标记；
4. **痛点诊断与营销话术没有来源**，由 `FALLBACK_OMIT_SECTIONS` 控制
   **整段不输出**（不是填「无」），最终四段：资费名称 / 资费摘要 / 切入话术 / 挽留话术。

**降级边界**：维表里没有该 `product_id` 或产品名称为空 →
`build_fallback_record()` 返回 `None` → 上层落 `code: "2"`；有行但没有任何
场景 ID 命中 IOP → 仍输出资费名称 / 摘要，切入与挽留段填「无」。
`fallback_reason` / `hit_label_id` / `hit_tag_index` 都写进 record 与 `__str__`，
便于日志排查。

> **口径变化**：原先「单个诊断维表缺库 / 空表 → 对应字段填『无』，其余照常」
> 已改为「任一查询空 → 整单走卖点维表兜底」，避免返回一屏「无」。

兜底时**不带【痛点诊断】【营销话术】**，其余四段样式一致（切入话术可含多个
`<br>` 行，拒绝原因与挽留话术按配对展示）：

```
<strong>【资费名称】</strong>升档<br>
<strong>【资费摘要】</strong>2G资费迁转19-10，含200分钟+11G国内<br>
<strong>【切入话术】</strong>基于客户标签特征切入（手厅近两个月新增注册用户）：……<br>基于问题引导的通用切入：……<br>
<strong>【挽留话术】</strong><strong><span style="color:blue;">价格太高：</span></strong>……<br>
<strong><span style="color:blue;">流量不够用：</span></strong>……
```

### 6. 待业务确认（均集中在 `diagnosis/constants.py`）

1. **单元格多值的正式分隔符**：默认竖线 / 全角竖线 / 换行，上游确认后改 `MULTI_VALUE_SPLIT_RE`。
2. **`RN` 方向**：默认小值优先，若口径相反改 `SMALLER_RN_HIGHER_PRIORITY`。
3. **切入话术降级开关**：默认允许无 RN 的交集场景补位；若要严格只认有 RN 的场景，改 `ALLOW_ENTRY_SCRIPT_FALLBACK`。
4. **`F09635` / IOP 标签**：该表不进主链路；样例话术里的 `#客运个性化运营标签34&` 一类占位符未做替换（缺映射依据）。
5. **F09631 / F09632 主键**：目前含话术全文以防丢行，若上游保证「一产品 / 一拒绝原因只有一条话术」，可收窄为纯业务键。
6. **兜底口径**：「任一查询空即整单兜底」比原来「单表为空只填『无』」更激进，若上游维表经常缺某张表、希望只降级对应字段，改 `query.py` 里对应的 `db_empty()` 检查即可。「IOP 无场景标签」的判定集中在 `fallback.has_scene_labels()`。
7. **tagN 解析口径**：默认把「切入话术」段全部非空行保留、「拒绝挽留话术」段按首个冒号拆成对，并去掉 `**`；若上游有更严格的分段规则，改 `fallback.parse_tag_script()` 一处即可。
8. **性能**：一次请求多个产品 ID 时会逐个查 7 张表，可在请求内加缓存（按请求隔离，不能放模块级）。

## IOP / 画像 / 卖点：由 `main.py` 注入、工厂按业务分发

本层不 import `IOP`（维表读取由本层 `dimensions.py` 承担）。请求内的编排在 `main.py`：

1. `need_labels(product_id)`（本层 `routing.py` 声明）决定该 ID 是否查标签，
   只有控损一类 ID 返回 `False`；
2. `IopLabelProvider` 在请求内惰性取一次标签，任何失败降级 `{}`；
3. 循环外调一次 `dimensions.load_selling_points(所有 productId)`，把
   `{product_id: SellingPointRow}` 作为 `selling_points` 传给
   `build_product_object()`；工厂按分支只转发：商机对象收标签与维表行并交给
   自己的 `AuxiliaryPortrait`，诊断取数收 IOP 标签与维表行，控损只收
   `record`，兜底只收维表行用于日志；`build_diagnosis_record()` 用 IOP 标签
   与维表行做兜底判定与取话术。

诊断 / 兜底也会触发 IOP：正常链路不消费标签，**兜底链路用 IOP 返回判断哪个
场景标签 ID 命中**（见第 5 节）。`routing.py` 把 `custom_label_info` 与
`selling_points` 一起传给 `build_diagnosis_record()`，在取数阶段消费完；诊断
对象只收结构化 `record`，不持有标签、维表行映射或画像组件。辅助画像只由商机
对象的 `AuxiliaryPortrait` 自行拼装，编排层不建画像缓存、工厂不传画像开关。

商机对象的 `AuxiliaryPortrait` 按维表行的 `label_ids` 对齐
`queried_label_ids` / `queried_label_nvs`（名称与值仍取 `custom_label_info`），
标签子集与画像都是懒执行；`selling_point_rows` 就是这份维表行映射（旧名
`product_selling_point_match_dict` 已废弃）。画像结果只缓存在组件实例上，不跨对象、不跨请求共享。
维表未命中 / 库缺失 → 标签子集为空、画像为空串。

## 降级速查

| 触发 | 行为 |
|---|---|
| 业务库缺失 / 查询异常 | `reader` 返回 `{}` / `[]`，业务对象走空记录分支，API 不 500 |
| 分表 AVL 的 `where` 缺标量 `product_no` | 定位不到分表，`reader` 返回 `[]`（不广播、不抛异常） |
| `F09633` 分表数被改动但未重灌全量 | 号码落到旧分表，查不到 → 该查询返回空 → 走卖点维表兜底 |
| 诊断 7 个查询任一返回空 / 场景交集为空 / 六项全空 | `query.py` 走卖点维表兜底，输出四段，`code: "0"` |
| IOP 返回里没有场景标签 | 同样走卖点维表兜底；无 ID 命中时切入 / 挽留段填「无」 |
| 维表里也没有该 `product_id` | 无法兜底 → 诊断分支返回 `None`，落 `code: "2"` |
| IOP 标签失败 | 由 `main.py` 降级 `{}` 注入；诊断侧按「无场景标签」触发兜底 |
| 卖点维表库缺失 / 表或列漂移 / 查询异常 | `dimensions` 降级 `{}`，商机辅助画像为空，话术其余段照常 |
| `product_id` 未命中维表 / 「标签ID」为空 | 标签子集为空、辅助画像为空串，不 500 |
| 维表写入侧故障 | 保留上次成功版本，在线侧下次读到的仍是它；不会让 API 500 |

## 自检与验证

```bash
python tests/check_diagnosis_offline.py      # 零第三方依赖主体，不连 SFTP / IOP
python -m call_stat --self-check            # 调用量统计组件自检
```

`check_diagnosis_offline.py` 用 8 个样例 AVL 建临时库 + 补一组业务键自洽数据，
断言注册 / 解析 / 入库、单一 flat 解析器、**F09633 分表（128 张表、号码不串表、
按 telnum 精确定位、缺 `product_no` 降级 `[]`；两侧分表口径一致）**、
**卖点维表 → 商机标签 ID（按 `db_config` 声明建临时 `dimensions.db`、换行 /
竖线拆分、`queried_label_ids` 与画像来自维表、库缺失降级）**、**诊断兜底
（1e 节解析 tagN / 场景标签判定 / 顺序遍历命中第二个 ID 即中断；5b 节 db 查空、
交集空、IOP 无场景标签三种触发，四段输出且无痛点 / 营销；维表无该 ID 仍
`code: "2"`）**、**业务包按业务名命名（1f 节：两个业务包可经 `services` 访问、
无历史别名、商机四段 HTML 逐字等价、控损组合标签解析等价；1g 节：控损 /
诊断 / 兜底不持有画像、标签、维表与缓存字段，旧属性名已移除）**，在线路由段
（第 7 段）再用 IOP 桩端到端验证「维表选 ID → IOP 取名值 → 辅助画像」与
「db 查空 → 在线侧 `code=0` 四段兜底」，多行查询、7 表关联、六段话术，结论
应为 `ALL PASS`。

脚本里写入侧配置（`load_profiles(runtime_dir=TMP)`，用于建库）与在线侧配置
（`db_config.load(avl_db_root=TMP/sqlite_db, ...)`，用于查询）**各自独立装配**，
共同指向同一批临时 db 文件；临时 `sftp_avl2db/.env` 只服务写入侧，结束后删除；
维表库用临时路径注入，不写包内 `runtime/`。

**验证结果 `ALL PASS`（126 项）**，不连 SFTP / IOP、不写包内 `runtime/`，
临时库跑完即删：

| 组 | 覆盖 |
|---|---|
| 注册 / 解析 / 入库 | 8 个诊断 AVL 各 10 行样例，按注册 DDL + `FlatColumnParser` 写库 |
| 单一 flat 解析器 | `PARSER_KINDS` 只有 flat / continuation；12 个 AVL parser 合法；商机 12 列 / 主键 (2,)；5 张诊断表主键下标按注册换算；末列原样保留文本（切入话术不退化成 0、商机 `"3"` 不被强转） |
| `F09633` 分表 | 声明为分表、分表键＝查询键、128 张表全建、号码只落在 `crc32` 指定的那张、按 `telnum` 精确定位、缺 `product_no` 降级 `[]` |
| 卖点维表 → 商机画像 | 按 `services/db_config.py` 的 `SELLING_POINTS_*` 声明建临时 `dimensions.db`；换行 / 竖线分隔的「标签ID」拆成保序去重元组；商机 `queried_label_ids` 与 `auxiliary_portrait` 来自维表（不再用固定集）；库缺失 / 未命中降级为空画像；在线路由段再用 IOP 桩端到端验证「维表选 ID → IOP 取名值 → 辅助画像」 |
| 诊断兜底（1e / 5b） | `tagN` 分段解析（切入 / 拒绝原因：挽留、去 `**`）、「匹配结果2」保序不去重、场景标签判定；**按顺序遍历命中第 2 个 ID 即中断**、取对应 tag1；db 查空 / 交集空 / IOP 无场景标签三种触发都输出四段且无痛点 / 营销；无 ID 命中时切入 / 挽留填「无」；维表无该 ID 仍 `None` |
| 业务包按业务名命名（1f） | `opportunity_recall/` / `loss_control/` 可经 `services` 访问、包入口显式导出语义化类名、无 `ProductInfoRewrite` 别名；商机四段 HTML 逐字锁定期望值、控损组合标签 / 独立颜色标签 / 无记录降级保持重构前行为 |
| 纯逻辑 | 【】拆分、无括号降级、ASCII `[]` 兼容、不等长多值配对、交集保留全部 `REASON`、RN 小者优先 |
| 多行 / 多条件查询 | IN 多行、多键 AND 含 IN、空 IN 短路、非法列名降级、`query()` 单条查询回归 |
| 7 表关联取数 | 资费名称/摘要来源、`BUSINESS_TYPE` 关联、交集场景、痛点汇总、RN 选切入话术、营销多行去重、挽留按位置配对、正常链路不触发兜底、维表也无该 ID → `None` |
| 业务对象 | `productName`、六段标签、实际话术内容；兜底对象四段输出 |
| 在线路由 | `services/routing.py` 返回 4 条：诊断 `code=0`、商机 `code=0`、db 查空的诊断兜底 `code=0` 四段、未知 ID `code=2`；IOP 桩端到端验证「维表选 ID → IOP 取名值 → 画像」 |

写入侧 12 个 AVL 复跑全部通过；落库值抽查：F09554/55/56 `city_id=3 (integer)`、
F09627 `rn=25 (integer)`、F09630 切入话术为完整文本。全仓 `.py` 编译零错误。

## 新增一类业务

1. 写入侧注册 AVL / 表结构（见 `sftp_avl2db/README.md`）；**同时在
   `services/db_config.py` 补一份在线侧声明**：`TABLE_SCHEMAS` 加列顺序、
   `AVL_DB_SPECS` 加 `db_file` / 查询键 / 排序 / 分表（分表数还要写进
   `.env.example`）。两侧各自维护，不互相引用，但列顺序与分表口径必须一致；
2. 在 `services/` 下按**业务名**加一个包（与 `diagnosis/` / `opportunity_recall/` /
   `loss_control/` 对齐），`rewrite.py` 里继承 `ProductInfoBase` 实现话术重组，
   构造只收本业务真正消费的参数；需要辅助画像才组合 `AuxiliaryPortrait`
   （IOP 标签默认就会查，只有控损一类 ID 在 `need_labels()` 里排除），不要把
   它们塞进公共基类，也不要在编排层加画像开关。
   `__init__.py` 显式导出语义化类名；不引入按 spec 编号的目录名或旧名别名；
3. `routing.py`：加静态映射（若需要）、在 `build_product_object()` 里按序插入分支、
   把新对象类加进 `BUSINESS_OBJECT_TYPES`；
4. `services/__init__.py` 加一条延迟导出；
5. 若新业务需要标签子集，在卖点维表 `selling_points` 里给对应 `product_id`
   配好「标签ID」列即可，代码不需要再加固定 ID 集；若也要诊断兜底，再配
   「产品名称」「匹配结果2」与对应的「标签N」话术单元格；
6. 更新本文件与根 README 的路由表、`tests/check_diagnosis_offline.py` 的断言。

## 文件与部署

本次在线链路涉及的文件：`services/README.md`（本文件）、
`services/db_config.py`、`services/.env.example`、
`services/{__init__,reader,dimensions,routing,product_info_raw}.py`、
`services/opportunity_recall/`、`services/loss_control/`、
`services/diagnosis/{__init__,constants,logic,query,fallback,rewrite}.py`、
`sftp_avl2db/parsers/{flat,__init__}.py`、
`sftp_avl2db/{avl_specs,config,table_schemas}.py`、
`sftp_avl2db/sink/router.py`、`main.py`、
`tests/check_diagnosis_offline.py`，以及 `sftp_avl2db/README.md`、
`local_excel2db/README.md`、`io_samples/README.md`、`tests/README.md`、
根 `README.md`、`解耦改造说明.md` 与归档文档 `诊断新线_在线链路实现说明.md`。

部署：

1. `cp sftp_avl2db/.env.example sftp_avl2db/.env` 并填 `SFTP_PASSWORD`；模板里每个 AVL 各有一整套 `<AVL>_*` 运行参数（轮询 / 清理 / 批次，按 db 粒度声明，没有项目级键），其中 `F09633` 另有 `F09633_NUM_TABLES=128`（其余 7 张维表仍是固定表）。改这个数必须停写并重灌 F09633 全量 AVL（不做历史单表迁移），见 [`sftp_avl2db/README.md`](../sftp_avl2db/README.md)「F09633 分表」。在线侧另 `cp services/.env.example services/.env`（不写则用 `db_config` 代码内默认值），分表数取值须两侧同步。
2. 诊断 8 个 AVL 各起一个常驻进程：`python -m sftp_avl2db --task F09627 … F09635`；库落 `sftp_avl2db/runtime/sqlite_db/diagnosis/`。
3. 卖点维表写入进程 `python -m local_excel2db` 维护 `dimensions.db`；在线侧经 `services/dimensions.py` 每请求只读一次，取商机标签 ID 与诊断兜底所需的产品名称 /「匹配结果2」/「标签N」。库未就绪时商机辅助画像为空、诊断无法兜底（维持 `code: "2"`），都不会 500。
4. 在线进程 `python main.py` 按 `services/.env`（或 `db_config` 代码内默认值）定位业务库与 `dimensions.db`；业务库查空 / IOP 无场景标签 / 场景交集为空时诊断走卖点维表兜底（四段话术 `code: "0"`），维表里也没有该 `product_id` 才落 `code: "2"`。
