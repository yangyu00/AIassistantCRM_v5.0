# services · 在线话术业务层

`services/` 是在线 API 的**业务层**：从业务库取数、按 `product_id` 选业务、
把原始记录重组成话术对象。`main.py` 只保留 HTTP 编排（入参校验、IOP 标签
提供者、响应组装），不再持有路由表，也不直接查库；辅助画像由商机对象自己
拼装，编排层不建缓存、不做画像判断。

> **职责边界（写入侧 / 在线侧彻底解耦）**
> - `sftp_avl2db/`：**纯写入侧**，SFTP AVL → SQLite，产出业务库；不 import 本层，也不声明在线侧的库路径、表字段或查询条件。
> - `local_excel2db/`：**卖点维表纯写入侧**，产出 `dimensions.db`；不 import 本层，也不提供查询入口。
> - `services/db_config.py`：**在线侧唯一的读取配置** —— 各 db 文件路径、表字段（含带类型的冗余声明）、查询键 / 排序、分表数、卖点维表列，全部自己维护；不 import 任何写入侧包。
> - `services/reader.py` / `services/dimensions.py`：两个只读模块，只从 `db_config` 取配置。
>
> 两侧唯一的交集是**磁盘上的 db 文件**（数据契约），没有任何代码 / 配置模块互相引用。表字段在两侧各写一份是刻意接受的冗余：读在线侧代码时不用跳到别的包就能看清 `SELECT` 回来的列顺序。改列顺序 / 分表数 / 库路径时两侧声明必须同步，见下文「业务库读取侧」与 [`sftp_avl2db/README.md`](../sftp_avl2db/README.md)「配置」。
>
> 全局架构、进程数、跨模块降级总表见[根 README](../README.md)；
> 诊断新线的字段口径与验证见下文「诊断新线」及 [`sftp_avl2db/README.md`](../sftp_avl2db/README.md)「诊断新线：8 张表注册」。

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
- 模块级 `_CONFIG = db_config.load()` + `db_config.validate(...)` 在**首次 import 本模块时**装配；`db_config` 支持没有 `services/.env` 的干净检出，`import services` 本身不触发。
- 离线打桩：`reader.configure(db_config.load(avl_db_root=临时目录, dimensions_db=临时维表库))`，把查询隔离到临时库，不碰包内 `runtime/`。

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
| 表 / 列名 | `SELLING_POINTS_TABLE` = `selling_points`；`SELLING_POINTS_SELECT_COLUMNS` 是线上实际 SELECT 的 34 列（2 必需 + 4 可选 + `tag_0 … tag_27`）。`SELLING_POINTS_SCHEMA` 多出 `product_category` / `product_subcategory` 两列，仅供离线自检按同一份声明建临时库，在线侧不读取 |
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

## 诊断新线（`services/diagnosis/`）

8 个 AVL（`F09627`～`F09633` / `F09635`）在 `db_config.AVL_DB_SPECS` 里各有
一份在线侧声明；主链路用其中 7 张表，`F09635`（标签ID-标签名）只保留声明、
不进链路。其中 `F09633` 是唯一带 `product_no` 的表（千万级行），按手机号分
128 张表，见上文「两个分表 AVL」。

| 文件 | 职责 |
|---|---|
| `constants.py` | 8 个 AVL 前缀、多值分隔符、括号正则、RN 方向、降级开关（待业务确认项集中在这里） |
| `logic.py` | 纯函数零 I/O：括号拆分、多值拆分、按位置配对、场景交集、优先级排序、HTML 拼装（`build_output_script(..., omit=())` 可整段省略） |
| `query.py` | `build_diagnosis_record(product_id, telnum, *, custom_label_info, selling_points)`，async，7 张表关联 + 兜底触发 |
| `fallback.py` | 卖点维表兜底：`parse_tag_script()` / `has_scene_labels()` / `build_fallback_record()`，纯函数零 I/O |
| `rewrite.py` | `ProductInfoByDiagnosis`，把 record 拼成 `output_script`；兜底 record 省略两段 |

正常链路输出六段固定：`【资费名称 / 资费摘要 / 痛点诊断 / 切入话术 / 营销话术 / 挽留话术】`，空值填「无」。

### 卖点维表兜底

**触发**（`query.py` 统一出口 `fallback(reason)`）：

| 条件 | `fallback_reason` |
|---|---|
| 7 个查询任一返回空（或提取出的有效值为空） | `db_empty:{AVL}` |
| IOP 返回里没有场景标签（口径见 `has_scene_labels()`） | `no_scene_labels` |
| 场景交集为空 | `empty_intersection` |
| 六项输出全空 | `empty_output` |

**兜底内容**（`fallback.py`，来源都是 `selling_points` 该行）：

1. `product_name` 按【】拆成资费名称 / 资费摘要 —— **与正常诊断一样**用
   `split_fee_name()`；响应 `productName` 只取 `record['business_name']` 原文
   （此处即维表「产品名称」），不再按【】拆分或回退；
2. 「匹配结果2」`match_result` 按位置拆成场景标签 ID，**顺序遍历**，第一个
   出现在 IOP `custom_label_info` 里的即命中并**中断遍历**；
3. 取同下标的「标签N」单元格，`parse_tag_script()` 按 `切入话术` /
   `拒绝挽留话术` 两个标题分段：前者收集非空行用 `<br>` 拼接，后者每行按首个
   全角/半角冒号拆成 `(拒绝原因, 挽留话术)`；解析时去掉 `**` 加粗标记；
4. **痛点诊断与营销话术没有来源**，由 `FALLBACK_OMIT_SECTIONS` 控制
   **整段不输出**（不是填「无」），最终四段：资费名称 / 资费摘要 / 切入话术 / 挽留话术。

**降级边界**：维表里没有该 `product_id` 或产品名称为空 → `build_fallback_record()`
返回 `None` → 上层落 `code: "2"`；有行但没有任何场景 ID 命中 IOP → 仍输出
资费名称 / 摘要，切入与挽留段填「无」。`fallback_reason` / `hit_label_id` /
`hit_tag_index` 都写进 record 与 `__str__`，便于日志排查。

> **口径变化**：原先「单个诊断维表缺库 / 空表 → 对应字段填『无』，其余照常」
> 已改为「任一查询空 → 整单走卖点维表兜底」，避免返回一屏「无」。

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

**「是否生成画像」不需要跨层传递**：只有商机对象持有 `AuxiliaryPortrait`，
工厂不给其它分支传画像参数，编排层也不建请求级画像缓存。组件按维表行的
`label_ids` 对齐 `queried_label_ids` / `queried_label_nvs`（名称与值仍取
`custom_label_info`），标签子集与画像都是懒执行；`selling_point_rows` 就是
这份维表行映射（旧名 `product_selling_point_match_dict` 已废弃）。画像结果只
缓存在当前组件实例上（话术拼装与日志各读一次），不跨对象、不跨请求共享。
维表未命中 / 库缺失 / IOP 降级 → 标签子集为空，`build_auxiliary_portrait()`
返回空串，商机话术不追加 `【辅助画像】`；控损 / 诊断 / 兜底连画像组件都没有。

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

## 自检

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
诊断 / 兜底不持有画像、标签、维表与实例缓存字段，旧属性名已移除）**，在线路由段
（第 7 段）再用 IOP 桩端到端验证「维表选 ID → IOP 取名值 → 辅助画像」与
「db 查空 → 在线侧 `code=0` 四段兜底」，多行查询、7 表关联、六段话术，结论
应为 `ALL PASS`。

脚本里写入侧配置（`load_profiles(runtime_dir=TMP)`，用于建库）与在线侧配置
（`db_config.load(avl_db_root=TMP/sqlite_db, ...)`，用于查询）**各自独立装配**，
共同指向同一批临时 db 文件；临时 `sftp_avl2db/.env` 只服务写入侧，结束后删除；
维表库用临时路径注入，不写包内 `runtime/`。

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
