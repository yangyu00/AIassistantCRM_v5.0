# services · 在线话术业务层

`services/` 是在线 API 的业务层：从业务库取数、按 `product_id` 选业务、把原始记录重组成话术对象。`main.py` 只保留 HTTP 编排（入参校验、IOP 标签提供者、响应组装）。`import services` 零 I/O。

## 1. 职责边界

| 包 | 角色 | 与 `services` 的关系 |
|---|---|---|
| `sftp_avl2db/` | 纯写入侧，产出业务库 | 不 import 本层；不声明在线侧库路径、表字段、查询条件 |
| `local_excel2db/` | 纯写入侧，产出 `dimensions.db` | 不 import 本层；不提供查询入口 |
| `services/db_config.py` | 在线侧唯一读取配置 | 独立维护 db 路径、表字段、查询键 / 排序、分表数、卖点维表列 |
| `services/reader.py` / `services/dimensions.py` | 两个只读模块 | 只从 `db_config` 取配置 |

两侧唯一交集是磁盘上的 db 文件（数据契约），没有任何代码 / 配置模块互相引用。表字段在两侧各写一份是刻意冗余：读在线侧代码时不用跳到别的包就能看清 `SELECT` 回来的列顺序。改列顺序 / 分表数 / 库路径时两侧声明必须同步。

全局架构、进程数、跨模块降级见[根 README](../README.md)；诊断字段口径与表注册见 [`sftp_avl2db/README.md`](../sftp_avl2db/README.md)。

## 2. 目录分层

| 层 | 文件 | 职责 |
|---|---|---|
| 包根 | `__init__.py` | 公共 API，`__getattr__` 延迟导出 |
| 读取配置 | `db_config.py` | `AVL_DB_SPECS`（12 个 AVL 的 db 文件、查询键、排序、分表）、`TABLE_SCHEMAS`（列顺序与类型）、`SELLING_POINTS_*`（卖点维表列）、`shard_index()` / `table_name()`、`load()` / `validate()` |
| 配置说明 | `.env.example` | 键注释模板；实际生效文件是随仓库提交的 `.env`，缺文件 / 缺键回落 `db_config.py` 代码内默认值 |
| 公共基类 / 画像组件 | `product_info_raw.py` | `ProductInfoBase`（最小公共形状）、`AuxiliaryPortrait`（商机专属，组合）、`ProductInfoRaw`（第 5 类兜底对象）、`build_auxiliary_portrait()` |
| 业务库读取侧 | `reader.py` | `query` / `query_many` / `query_in`；只认 AVL 前缀 |
| 卖点维表读取侧 | `dimensions.py` | `load_selling_points()` / `split_label_ids()`；`product_id → label_ids / product_name / selling_point / fallback_script / match_result / tag_*` |
| 路由 | `routing.py` | `product_id → AVL` 两张静态表、`need_labels()`、`build_product_object()` |
| 商机回捞 | `opportunity_recall/` | 四段话术 + 辅助画像 |
| 控损 | `loss_control/` | `strategy_name` 自定义标签 → HTML；`parse_custom_tags()` 为模块级纯函数 |
| 诊断 | `diagnosis/` | **只认 8 张表**：取数、纯逻辑、六段话术对象、口径常量；不读卖点维表、不做兜底 |
| 卖点维表话术 | `selling_point_script/` | **与诊断平级的第四类业务**：消费维表一行 + 请求级 IOP 返回 + `label_name_mapping`，复现上一版 `ProductInfoRewrite` 的 HTML 形状（唯一差异：出口把全部换行转成 `<br>`）；字段抽取与话术组合各自单点可改；不读诊断 8 表、不做 `#标签名&` 替换 |

四类业务包以业务名命名，包内 `rewrite.py` 承载业务对象、`__init__.py` 显式导出。诊断与卖点话术**互不 import**：诊断的话术拼接只需要 8 张表，卖点话术的组合只需要维表一行，两边的数据源与字段集完全不重叠。

**对象模型**：五类对象（四类业务 + 第 5 类兜底）只继承极简 `ProductInfoBase`（`product_id` / `product_name` / `output_script` + 统一日志头）；辅助画像用组合挂在商机对象的 `AuxiliaryPortrait` 上，结果只缓存在该组件实例上。控损 / 诊断 / 卖点话术 / 兜底对象不持有画像、标签、维表属性。基类通过 `_extra_str_lines()` 钩子统一拼装公共日志行。

| 业务 | 对外 API | 定义位置 |
|---|---|---|
| 商机回捞 | `ProductInfoByOpportunityRecall` | `opportunity_recall/rewrite.py` |
| 控损 | `ProductInfoByLossControl` | `loss_control/rewrite.py` |
| 诊断 | `ProductInfoByDiagnosis` / `build_diagnosis_record()` | `diagnosis/rewrite.py` / `query.py` |
| 卖点维表话术 | `ProductInfoBySellingPoint` / `build_selling_point_object()` | `selling_point_script/rewrite.py` |

## 3. 五类路由（四类业务 + 兜底，顺序不可调换）

`build_product_object(product_id, telnum, *, custom_label_info, selling_points, label_name_mapping=None)` 是唯一对象工厂；`main.py` 用 `isinstance(obj, BUSINESS_OBJECT_TYPES)` 判 `code`（`"0"` 带话术 / `"2"` 空话术）。

| 顺序 | 条件 | 数据源 | 对象 |
|---|---|---|---|
| 1 | 商机回捞 3 个 ID：`202608041615335173/172/174` → `F09554/F09555/F09556` | `reader.query(avl, telnum)` | `ProductInfoByOpportunityRecall` |
| 2 | 控损 1 个 ID：`202609180955095402` → `F09591`，命中即返回 | `reader.query(avl, telnum)` | `ProductInfoByLossControl` |
| 3 | 诊断（由 `F09629` 动态命中，无静态 ID 白名单） | `diagnosis.build_diagnosis_record()`：8 表（7 表关联 + `F09635` 标签反查）；任一环节为空返回 `None` | `ProductInfoByDiagnosis` |
| 4 | 诊断返回 `None`（或前面都没命中） | `selling_point_script.build_selling_point_object(product_id, selling_points, custom_label_info, label_name_mapping)`：只消费维表一行 + 请求级 IOP 返回 + 标签显示名映射 | `ProductInfoBySellingPoint` |
| 5 | 其余 | — | `ProductInfoRaw`（兜底，`code: "2"`） |

- 业务库未就绪（`reader.query` 返回 `{}`）时按空记录出话术：商机仍输出四段（各段填「无」），控损输出「无历史话术记录」，都不落到诊断 / 卖点话术 / 兜底，也不抛 500。
- 诊断不维护静态映射：`F09629`（`DIM_MARKET_PRODUCT_BUSINESS_ONCE`）本身即「产品ID → 策略 / 产品」映射表。**诊断只认自己的 8 张表**：7 表关联任一为空、场景交集为空、六项输出全空时返回 `None` 并打一行 WARNING，由路由层继续询问平级的卖点话术模块，诊断自己不拼任何维表话术。
- 卖点话术模块只按 `product_id` 取维表一行：**维表里没有该 `product_id`，或有该行但 `product_name` 为空**时返回 `None`，才落第 5 类 `code: "2"`（边界见第 6 节）。
- `product_id → AVL 前缀` 的两张静态表在 `routing.py` 唯一维护；`reader` 只按 `db_config` 声明的 `{project}/{AVL}.db` 定位。
- `selling_points` 由 `main.py` 在循环外调一次 `dimensions.load_selling_points()` 批量查回，只透传给对象工厂，不参与路由判定；诊断分支只收 `custom_label_info`，卖点话术分支收维表行 + `custom_label_info` + `label_name_mapping`（见第 7 节）。

## 4. 业务库读取侧：`reader.py` + `db_config.py`

`db_config.py` 独立维护：

- `AVL_DB_SPECS`：12 个 AVL 各自的 `db_file`（如 `diagnosis/F09633.db`）、schema 名、单条查询键 `where`、固定 `order_by`、是否分表。
- `TABLE_SCHEMAS` / `TABLE_PRIMARY_KEYS`：各表列顺序 / 类型（与写入侧建表 DDL 对齐）与主键；`SELECT *` 回来的 dict 按此列顺序组装。
- `services/.env`：共 7 键——`AVL_DB_ROOT`、`DIMENSIONS_DB`、`LOSS_CONTROL_NUM_TABLES`、`F09633_NUM_TABLES`，以及按业务分别配置的新鲜度窗口 `OPPORTUNITY_RECALL_FRESHNESS_WINDOW_DAYS`（默认 `2`）、`LOSS_CONTROL_FRESHNESS_WINDOW_DAYS`（默认 `2`）、`DIAGNOSIS_FRESHNESS_WINDOW_DAYS`（默认 `0`，关闭过滤，见第 4.1 节）；缺文件 / 缺键用 `db_config.py` 顶部代码内默认值（默认指向 `sftp_avl2db/runtime/sqlite_db` 等）。只认这三个业务键。
- `shard_index()` / `table_name()`：分表号 `crc32(product_no) % NUM_TABLES`，表名 `{AVL}_{分表号}`；固定表名为 `{AVL}`。

三个入口都是 `async`、内部同步 `sqlite3`，列名走 `DbProfile.columns` 白名单、值一律参数绑定，排序固定取静态声明：

| 入口 | 返回 | 语义 |
|---|---|---|
| `query(avl, key)` | `dict` | 单条，`op_time DESC LIMIT 1`；商机 / 控损使用 |
| `query_many(avl, *, where, limit=None)` | `list[dict]` | `where={列名: 标量｜序列}`，标量 `=`、序列 `IN`，多键 AND |
| `query_in(avl, column, values, *, limit=None)` | `list[dict]` | 单列 IN 便捷形式 |

- 库不存在 / 表缺失 / AVL 未注册 / 非法列名 / 查询异常 → 降级 `{}` 或 `[]`，不抛给在线接口；空 IN 集合直接短路返回 `[]`。
- 分表 AVL 需标量 `product_no` 定位分表；缺它或传序列 → 降级 `[]`（不广播到 128 张表）。批量号码请按分表键分组后逐个调用。
- 模块级 `_CONFIG = db_config.load()` + `db_config.validate(...)` 在首次 import 本模块时装配；`db_config` 支持没有 `services/.env` 的干净检出。
- 离线打桩：`reader.configure(db_config.load(avl_db_root=临时目录, dimensions_db=临时维表库))`。

#### 4.1 AVL 批次新鲜度窗口

`INSERT ... ON CONFLICT DO UPDATE`（upsert）只覆盖「本批出现过的主键」，上游不再下发的主键会带着旧 `op_time` 留在库里。在线侧按业务各自配置一个新鲜度窗口，把这些过时行挡在查询外：

| 项 | 位置 | 说明 |
|---|---|---|
| 批次水位 | 写入侧在每个 `{AVL}.db` 内维护的 `_avl_meta` 表 | 一行 / AVL：`latest_file_date`（文件名日期）、`latest_op_time`（该批数据最大 `op_time`）、`last_filename`、`updated_at`；只在「新文件日期 ≥ 已记录日期」时推进，补跑旧文件不回退 |
| 变量 `OPPORTUNITY_RECALL_FRESHNESS_WINDOW_DAYS` / `LOSS_CONTROL_FRESHNESS_WINDOW_DAYS` / `DIAGNOSIS_FRESHNESS_WINDOW_DAYS` | `services/.env`（默认 `2` / `2` / `0`，`0` 关闭） | 按业务（project）分别生效：`N` = 只召回 `op_time ∈ [最新批次-(N-1)天, 最新批次]` 的数据。商机回捞 / 控损 `N=2`，诊断 8 张表 `N=0` 关闭过滤、不做批次收敛。以数据里的 `op_time` 为基准，不依赖「`op_time` = 文件日期 - 1」这一当前事实 |

- **单条** `query()`：`op_time >= 下界` + `ORDER BY op_time DESC LIMIT 1`（窗口内取最新一条）。
- **多条** `query_many()` / `query_in()`：先按窗口过滤，再在**结果集内**收敛到 `op_time = MAX(op_time)` 的那一批。例：最新批次 21 日、`N=2`（下界 20 日），结果里同时有 20 / 21 日 → 只返回 21 日集合。
- 下界由 `_cutoff()` 按当前 AVL 的 `project` 取对应业务的 `N` 计算；水位表缺失 / 无行 / 日期格式异常 / 该业务 `N=0` 一律降级为不过滤。`read_config.freshness_window_days_for(project)` 是唯一取数点；`load(freshness_window_days=单值)` 只是离线自检的编程式注入（把全部业务临时设成同一窗口），不是 `.env` 键。
- `_from_env()` 只遍历 `FRESHNESS_WINDOW_ENV_KEYS` 里的三个业务键，其它键一律忽略；缺键用代码内默认值。
- 水位与业务查询在**同一个 sqlite 连接**里读取，每请求不增加额外连接；`_avl_meta` 不进分表布局，也不被 `purge_expired()` 清理。
- 自检：`python tests/check_avl_freshness.py`。

两个分表 AVL：

| AVL | 业务 | 分表键 | 在线查询键 | 分表数（`db_config` 声明） |
|---|---|---|---|---|
| `F09591` | 控损 | `product_no` | `product_no`（手机号） | `LOSS_CONTROL_NUM_TABLES`（默认 128） |
| `F09633` | 诊断·用户场景对应关系 | `product_no` | `product_no`（`telnum`） | `F09633_NUM_TABLES`（默认 128） |

`F09633` 是诊断 8 表里唯一含手机号的表，千万级行；分表键与 `build_diagnosis_record()` 的 `where={"product_no": telnum}` 一致，一次请求只命中一张分表。写入侧按同一口径落库。

商机回捞 12 列 / 控损 5 列的完整列名、类型、主键与解析消费字段见 [`sftp_avl2db/README.md` 第 7 节](../sftp_avl2db/README.md)；本层 `db_config.TABLE_SCHEMAS` 与写入侧 `table_schemas.TABLE_SCHEMAS` 逐列同序，改列顺序时两侧必须同步。

## 5. 卖点维表读取侧：`dimensions.py`

`main.py` 每请求读两次 `dimensions.db`（各 1 次 `mode=ro` 连接、无进程级缓存）：`load_selling_points(product_ids)` 按本次全部 `productId` 批量 `IN` 查 `selling_points`，返回 `{product_id: SellingPointRow}`；`load_label_name_mapping()` 全表读 `label_name_mapping`，返回 `{label_id: 营销助手显示名字}`。

| 项 | 口径（均在 `db_config.py` 声明） |
|---|---|
| 库 | `DIMENSIONS_DB`（默认 `local_excel2db/runtime/sqlite_db/dimensions.db`） |
| 连接 | `sqlite3` + `mode=ro` 只读 |
| 表 / 列名 | `SELLING_POINTS_TABLE` = `selling_points`；`SELLING_POINTS_SELECT_COLUMNS` 为线上实际 SELECT 的 34 列（2 必需 + 4 可选 + `tag_0 … tag_27`）。`SELLING_POINTS_SCHEMA` 多出 `product_category` / `product_subcategory`，仅用于离线自检建临时库 |
| 商机画像用列 | `product_id`（主键）→ `label_id`（Excel「标签ID」） |
| 卖点话术用列 | `product_name`（产品名称 → 响应 `productName`）、`selling_point`（「生成话术」原文 → 【卖点】段）、`fallback_script`（「兜底话术」原文 → 切入 / 挽留段）、`match_result`（「匹配结果2」，决定标签专属话术是否可用）、`tag_0 … tag_27`（「标签N」原文） |
| 标签显示名 | `LABEL_NAME_MAPPING_TABLE` = `label_name_mapping`；`LABEL_NAME_MAPPING_COLUMNS` = `(label_id, display_name)`，由「标签转换维表.xlsx」写出 |
| 其它列 | 可选列缺失填空串；库里所有话术 / 标签列都只存原文，不做预解析 |
| 只存原始文本 | 库里所有话术 / 标签列都是 `TEXT` 原文；`split_label_ids()`、`cut_in_str2tuple()`、`build_selling_point()` 等格式转换都在 `selling_point_script/logic.py` |

`SellingPointRow` 只把「标签ID」拆成 `label_ids`；`fallback_script` / `tag_texts` / `match_result` 都是库里的原始文本。

- 每请求 2 次只读连接（`selling_points` 按 ID 批量、`label_name_mapping` 全表）、不做进程级缓存：写入侧按固定间隔整表覆盖，缓存会让新维表不生效；同一请求内靠返回的 dict 复用，重复 ID 只查一次。
- 列名走 `db_config` 白名单、值一律参数绑定，IN 列表按 500 分块。
- `split_label_ids()`：换行 / 竖线 / 逗号 / 分号（含全角）→ 去空、保序、去重。
- 库不存在 / 表缺失 / 必需列漂移 / 查询异常 → 打日志并降级 `{}`；未命中的 `product_id` 不在返回 dict 中。商机辅助画像为空，卖点话术模块无行可用（落 `code: "2"`）；`label_name_mapping` 表缺失 / 列漂移同样降级 `{}`，此时辅助画像直接用 IOP 原名，输出结构不变。都不抛给在线接口。
- 离线打桩：`dimensions.configure(临时 dimensions.db)`，传 `None` 还原默认路径。

分工：商机侧由维表 `label_id` 决定「查哪些 ID」、IOP 返回的 `custom_label_info` 决定「名称与值」；**卖点话术侧**消费维表原文 + IOP 返回 + `label_name_mapping`：IOP 返回拼【辅助画像】并决定命中哪组标签专属话术，映射把 IOP 的 `ITEMNAME` 换成营销助手显示名；按上一版 `ProductInfoRewrite` 的格式拼出「辅助画像 / 切入话术 / 卖点 / 挽留话术」，原文里不存在待替换字段，**不做 `#标签名&` → IOP `ITEMVALUE` 替换**，也不读诊断 8 张表；**诊断侧**完全不读维表。db 里已转好的列是唯一数据源。写入侧配置见 [`local_excel2db/README.md`](../local_excel2db/README.md)，在线侧列映射不取自它。

## 6. 诊断与卖点话术（两个平级业务包）

### 6.1 诊断（`services/diagnosis/`）：只认 8 张表

8 个 AVL（`F09627`～`F09633` / `F09635`）在 `db_config.AVL_DB_SPECS` 里各有一份在线侧声明；主链路用其中 7 张表关联取话术，第 8 张 `F09635`（标签ID-标签名，主键 `tag_name`）按需按标签名反查 `tag_id`（= IOP 的 `ITEMCODE`），配合请求级 IOP 标签替换话术里的 `#标签名&`。`F09633` 是唯一带 `product_no` 的表，按手机号分 128 张表。

| 文件 | 职责 |
|---|---|
| `constants.py` | 8 个 AVL 前缀、多值分隔符、括号正则、标签占位符正则、RN 方向、切入话术降级开关 |
| `logic.py` | 纯函数零 I/O：括号拆分、多值拆分、按位置配对、场景交集、优先级排序、标签占位符收集 / 替换（`collect_tag_names` / `replace_tag_placeholders` / `apply_tag_values`）、HTML 拼装（`build_output_script()`，诊断固定六段） |
| `query.py` | `build_diagnosis_record(product_id, telnum, *, custom_label_info)`，async，7 表关联 + 第 8 张表 F09635 反查 + IOP 值替换；任一环节为空返回 `None`，不读维表、不拼兜底。不 import IOP，标签由编排层注入 |
| `rewrite.py` | `ProductInfoByDiagnosis`，把 record 拼成固定六段 `output_script` |

**诊断不消费 `selling_points`，也不导出任何兜底话术符号。**

#### 7 表关联取数 + 话术标签值替换

1. `F09629` 按 `product_id` 取 `BUSINESS_NAME` / `BUSINESS_TYPE`；`BUSINESS_NAME` 按【】内外拆成资费名称 / 资费摘要。
2. `F09628` 按 `business_type` 取产品场景集合（多行）。
3. `F09633` 按 `product_no`（标量，同时是分表键）取用户场景与痛点诊断，单元格多值按位置配对。
4. 用户场景 ∩ 产品场景，保留全部交集场景的 `REASON` 作为痛点诊断。
5. 交集场景按 `F09627` 的 RN 排优先级（默认 RN 越小优先级越高），取第一个能取到 `F09630` 切入话术的场景；`ALLOW_ENTRY_SCRIPT_FALLBACK` 开启时允许无 RN 的交集场景按原顺序补位。
6. `F09631` 按 `business_type` 取营销话术（多行，去重）。
7. `F09632` 取 `reject_reason` / `wl_huashu`，多值按位置配对成挽留话术列表。

**话术标签值替换（只作用于成功 record）**：`collect_tag_names()` 从「切入话术 / 营销话术 / 挽留话术（拒绝原因 + 话术）」里收集 `#标签名&`，去重后一次 `query_many(F09635, where={"tag_name": [...]})` 拿到 `{标签名: tag_id}`；`tag_id` 即 IOP 的 `ITEMCODE`，用 `custom_label_info[tag_id]` 的 `[ITEMNAME, ITEMVALUE]` 取 `ITEMVALUE`，再由 `apply_tag_values()` 写回 record 的三个话术字段。标签名未登记、ITEMCODE 不存在、`ITEMVALUE` 为 `None` 或 IOP 整体降级 `{}` 时，该占位符原样保留，不静默吞掉。`F09635` 主键与在线查询键均为 `tag_name`，与写入侧声明一致。

正常链路输出六段固定：`【资费名称 / 资费摘要 / 痛点诊断 / 切入话术 / 营销话术 / 挽留话术】`，空值填「无」。

#### 诊断不适用（返回 `None`，由路由衔接平级模块）

| 条件 | 日志原因 |
|---|---|
| 7 个查询任一返回空（或提取出的有效值为空） | `db_empty:{AVL}` |
| 场景交集为空 | `empty_intersection` |
| 六项输出全空 | `empty_output` |

`query.py` 的统一出口 `_miss(reason)` 打一行 WARNING 后返回 `None`；原因只进日志，不带进任何业务对象。`routing.build_product_object()` 收到 `None` 后继续第 4 步，交给 `selling_point_script.build_selling_point_object()`。因此「诊断失败」与「卖点话术怎么拼」彻底解耦：改维表抽取字段 / 组合方式不会动到诊断代码，改 8 表口径也不会动到卖点话术代码。

### 6.2 卖点维表话术（`services/selling_point_script/`）：与诊断平级

把 db 里已转好的维表信息，经过转换输出话术格式。
只用 ``SellingPointRow`` 的 ``TEXT`` 原文做格式转换；HTML 模板逐字符对齐。

| 文件 | 职责 |
|---|---|
| `logic.py` | 纯函数零 I/O：`cut_in_str2tuple()`（切入 / 挽留话术分段 + 蓝色 span）、`build_selling_point()`（卖点标红 / 加粗）、`strong_sections()`（标题加粗）、`build_output_script()`（**唯一组合点**，末尾把全部换行转 `<br>`）、`select_script()`（按标签选话术）、`extract_selling_point_fields()`（**唯一抽取点**） |
| `rewrite.py` | `ProductInfoBySellingPoint`（业务对象）+ `build_selling_point_object(product_id, selling_points, custom_label_info=None, label_name_mapping=None)`（工厂，无行 / 产品名为空返回 `None`） |
| `__init__.py` | 显式导出语义化类名与两个单点函数 |

**db 字段 → 上一版输出位置**

| db 列（Excel 源列） | 转换后落在哪 |
|---|---|
| `product_name`（产品名称） | 响应 `productName` 原文；**不进** `output_script` |
| `label_id`（标签ID） | 标签子集 → 对齐 IOP 返回与 `label_name_mapping`，拼【辅助画像】 |
| `selling_point`（生成话术） | 【卖点】段 |
| `fallback_script`（兜底话术） | 【切入话术】/【挽留话术】段的默认来源 |
| `match_result`（匹配结果2） | 为空且「兜底话术」为空、「标签0」非空时保留「兜底话术」组（回退「标签0」原文），其余为空值情况不输出切入 / 挽留；非空时按下标对应 `tag_g` 原文 |
| `tag_0 … tag_27`（标签N） | 命中对应场景 ID 时的标签专属话术 |

**输出格式（逐字符）**

1. 四段固定顺序：`辅助画像 → 切入话术 → 卖点 → 挽留话术`；**空段整段不出现**（不是填「无」）。
2. 中间层段间仍按真实换行 `\n` 拼接，但**唯一组合出口把全部 `\n` 转成 `<br>`**（与商机 / 控损一致），最终 `output_script` 不含真实换行；最后一段不带尾换行；`【xxx】` 标题统一包 `<strong>…</strong>`。
3. 切入 / 挽留：`cut_in_str2tuple()` 用 `r'切入话术(.*?)拒绝挽留话术(.*)'`（DOTALL）切两段，各自加 `【切入话术】\n` / `【挽留话术】\n`；每行先 `strip()` → `:` 换 `：` → 按 `：` 分割，第一段去掉 `^\d+\s?\.\s?` 后包成 `<strong><span style="color: blue;">前缀：</span></strong>`（**`color: blue;` 带空格**），再拼回其余段；行间 `\n`。调用前先去掉全部 `**`。
4. 卖点：`build_selling_point()` 用 `r'\d、.*?：'`（DOTALL）找 `1、资费：` / `2、价格：` / `3、业务确认：` 等标题；首段内容包 `<span style="color:red;">…</span>`（**无空格**）、首标题一起包 `<strong>`；后续每段 `\n<strong>标题：</strong>内容.strip()`。无标题时原文原样跟在 `【卖点】\n` 后。
5. **不做 `#标签名&` 字段替换**：不收集占位符、不查 `F09635`，话术原文逐字符输出——这是与诊断链路的关键区别（诊断三类话术才做替换）。IOP 返回另有用处：拼【辅助画像】、按「匹配结果2」选标签专属话术，二者都不改动话术原文里的字符。
6. 降级边界：维表无该 `product_id` 或 `product_name` 为空 → 工厂返回 `None` → 路由落 `ProductInfoRaw`（`code: "2"`）。
7. **换行出口转换**：`parse_fusion_scripts()` / `build_selling_point_section()` 等中间函数仍返回含 `\n` 的文本；只有 `build_output_script()` 在标题加粗后做 `.replace("\n", "<br>")`，与商机 / 控损一致。离线自检的 `EXPECTED_LEGACY_OUTPUT` 与独立参照 `legacy_output()` 已同步到该口径。

**取数口径（仍只从 db 列重建）**

* 「匹配结果2」为空 → 仅当「兜底话术」为空且「标签0」非空时，话术字典保留「兜底话术」组（值为「标签0」原文）；其余情况该 pid 的话术字典是 `{}`，切入 / 挽留整段不出现。
* 「兜底话术」列为空 → 回退「标签0」原文充当兜底话术组；不回退其它「标签N」，也不取第一个非空「标签N」。
* 同一场景 ID 在「匹配结果2」里重复出现时，后一次覆盖前一次。
* **IOP 返回由路由总是传入**：按「匹配结果2」的 ID 顺序找第一个 `ITEMVALUE ∈ {是, 1}` 的标签，用对应「标签N」原文；同时按维表「标签ID」子集拼【辅助画像】。
* 辅助画像**完整复现上一版 `ProductInfoRaw` 的转换规则**：`label_name_mapping` 换显示名、`0.00→无`、费用加「元」、抖音快手流量加「M」、`是否` / `1` → 是 / 否、降档阈值名称标红、同名标签去重（含上一版 `replace` 未回写的行为）。离线自检用独立参照实现做逐字符 golden 比对。
* `custom_label_info` / `label_name_mapping` 缺失时，辅助画像为空、场景不命中，切入 / 挽留落「兜底话术」，输出结构不变。

以后要「改变从卖点维表中抽取的信息」只改 `extract_selling_point_fields()`，要「进行不同的组合」只改 `build_output_script()`，两者互不影响，也不会牵动诊断。

### 6.3 实现级口径

仅靠上面的概要无法复现逐字符行为，下面是代码里的精确口径。

**常量（`services/diagnosis/constants.py`）**

| 项 | 值 |
|---|---|
| 8 个 AVL 前缀 | `F09627` 场景优先级、`F09628` 产品-场景、`F09629` 产品策略、`F09630` 场景-切入话术、`F09631` 产品-营销话术、`F09632` 产品-挽留话术、`F09633` 用户场景（分表）、`F09635` 标签ID-标签名（主键 / 查询键 `tag_name`，按标签名反查 `tag_id` 供话术占位符替换） |
| `TAG_PLACEHOLDER_RE` | `#([^#&]+)&`（`#` 开头、`&` 结尾包裹标签名；中间不含 `#` / `&`，避免跨占位符贪婪匹配） |
| `SMALLER_RN_HIGHER_PRIORITY` | `True`（RN 越小优先级越高） |
| `ALLOW_ENTRY_SCRIPT_FALLBACK` | `True`（允许无 RN 的交集场景按原顺序补位） |
| `EMPTY_TEXT` | `无` |

两条含竖线的正则放在表格外以免与 Markdown 列分隔符冲突；`TAG_PLACEHOLDER_RE` 见上表，这里一并列出：

- `MULTI_VALUE_SPLIT_RE` = `[|｜\n]+`（竖线 / 全角竖线 / 单元格内换行；**不含逗号、分号**）
- `FEE_BRACKET_RE` = `【([^】]*)】|\[([^\[\]]*)\]`（同时支持 ASCII `[]`）
- `TAG_PLACEHOLDER_RE` = `#([^#&]+)&`（`#` / `&` 包裹标签名；中间不含 `#` / `&`）

**诊断纯函数口径（`diagnosis/logic.py`）**

- `split_fee_name(text)`：`strip()` 后无括号 → `("", 全文)`；有括号 → 名称取 `group(1) or group(2)`，摘要为**用该正则删掉全部括号**后的剩余文本再 `strip()`。样例 `【五大重点客群】不限量客户10元享40G阈值扩容` → `("五大重点客群", "不限量客户10元享40G阈值扩容")`。
- `split_multi()` → 去空白、去空项；`pairs_by_position()` 用 `zip_longest(..., fillvalue="")` 按相同下标配对。
- `dedup()` 按首次出现顺序去重、跳过空值；`extract_scripts(rows, column)` 取列、`strip()` 后 `dedup`。
- `expand_user_scenes(rows)`：只收 `scene` 非空的 `(scene, reason)`；`intersect_scenes()` 保留交集首次出现顺序，同一场景可贡献多条非空 `REASON` 作为痛点诊断。主链路 `query.py` 在拿到 `pain_points` 后再过一次 `dedup()`：上游全量 AVL 保证 `(product_no, scene)` 唯一，这一步是上游偶发重复行的兜底，按首次出现去重、不误伤不同文案。
- `order_scenes_by_priority(scenes, rank_rows)`：`rn` 只能 `int()` 成功才入 ranks；排序键有 RN 为 `(0, rn, index)`、无 RN 为 `(1, 0, index)`；`allow_fallback=False` 时才过滤掉无 RN 场景。返回 `(ordered, ranked_set)`。
- `expand_retain_pairs(rows)`：按位置配对后**只收 `huashu` 非空**的 `(拒绝原因, 挽留话术)`。
- `pick_retain_pair(retain_pairs, *, rng=None)`：`retain_pairs` 空 → `None`；否则随机返回一条 `(拒绝原因, 挽留话术)`，按「条」抽取、不拆开配对；`rng` 可注入 `random.Random(seed)` 复现，缺省走全局 `random`。
- `build_output_script(record, *, omit=(), rng=None)`：每段 `line(label, value)` = `<strong>【{label}】</strong>{value or 无}`；痛点 / 营销为多行 `<br>` 拼接；**挽留段只输出 `pick_retain_pair` 随机选中的一条**，单行 `<strong><span style="color:blue;">{reason or 无}：</span></strong>{huashu or 无}`，空召回 → 空段；六段按固定顺序 `<br>` 连接，`omit` 中的段整段不出现。诊断对象调用时不传 `omit`，固定六段。

**7 表关联的精确调用（`diagnosis/query.py`）**

标签值替换走同一模块的 `_load_tag_values()` / `finalize()`：按 `tag_name` 查 F09635 后对齐 IOP 的 `ITEMCODE`，不计入下面 7 步关联；只有成功 record 在返回前经过它。

| 步 | 调用 | 空结果处理 |
|---|---|---|
| 1 | `query_many(F09629, where={"product_id": product_id}, limit=1)` | `db_empty:F09629` |
| 2 | `query_many(F09628, where={"business_type": business_type})` → `dedup(scene)` | `db_empty:F09628` |
| 3 | `query_many(F09633, where={"product_no": telnum})` → `expand_user_scenes` | `db_empty:F09633` |
| 4 | `intersect_scenes(user_pairs, product_scenes)` → `dedup(pain_points)` | 交集为空 → `empty_intersection`；痛点文案按首次出现去重 |
| 5 | `query_in(F09627, "scene", matched_scenes)` → `order_scenes_by_priority` → `query_in(F09630, "scene", ordered_scenes)`；`qr_map` 只收 `qr_huashu` 非空；`top_scene` 取 `ordered_scenes` 里第一个能查到话术的场景，`top_scene_source` ∈ `rank` / `fallback` / `none` | 任一查询结果为空 → `db_empty:F09627` / `db_empty:F09630` |
| 6 | `query_many(F09631, where={"business_type": business_type})` → `extract_scripts` | `db_empty:F09631` |
| 7 | `query_many(F09632, where={"business_type": business_type})` → `expand_retain_pairs` | `db_empty:F09632` |

record 字段固定为 `product_id / telnum / business_name / business_type / fee_name / fee_summary / product_scenes / user_scenes / matched_scenes / pain_points / top_scene / top_scene_source / entry_script / marketing_scripts / retain_pairs`。`pain_points` 在主链路已按首次出现去重（上游同键重复行的兜底）。六项输出 `fee_name, fee_summary, pain_points, entry_script, marketing_scripts, retain_pairs` 全空时 → `empty_output`、返回 `None`。`retain_pairs` 始终保留 F09632 的全量召回（标签替换对全集生效、日志也打印全集），「随机输出一条」只发生在 `build_output_script` 的拼装阶段。

**卖点话术格式转换（`selling_point_script/logic.py`）**

| 项 | 值 |
|---|---|
| 段落标题 | 正则 `r'切入话术(.*?)拒绝挽留话术(.*)'`（`re.DOTALL`）；两段分别加 `【切入话术】\n` / `【挽留话术】\n` |
| 原文清洗 | 调用处 `str(text).strip().replace("**", "")`：去掉全部 markdown 加粗标记 |
| 行处理 | 空行跳过；`strip()` → `:` 换 `：` → `split("：")`；单段原样；多段首段去 `^\d+\s?\.\s?` 后包 `<strong><span style="color: blue;">{首段}：</span></strong>`，其余用 `：` 拼回 |
| 段内拼接 | `'\n'.join(行)`；解析结果两段都空 → 返回 `""`（字符串） |
| 卖点标题 | `r'\d、.*?：'`（`re.DOTALL`）findall；首段 `start_index` 后内容标红，后续按标题 `re.split` 逐段 `\n<strong>标题</strong>内容.strip()` |
| 原文来源 | `pick_script_text()` 取 `fallback_script.strip()`，为空时回退「标签0」原文；`build_script_dict()` 在「匹配结果2」非空时按 `tag_g` 下标补标签专属话术，为空且回退到「标签0」时保留「兜底话术」组、否则返回 `{}` |
| IOP 辅助画像 | `build_auxiliary_portrait(label_ids, custom_label_info, label_name_mapping)` 直译上一版 `ProductInfoRaw` 的全部硬编码转换 |
| 标题加粗 | `strong_sections()` 把 `【辅助画像】/【切入话术】/【卖点】/【挽留话术】` 全部 `replace` 成 `<strong>…</strong>` |
| 字段替换 | **无**。不收集 `#标签名&`、不查 F09635；IOP 只用于辅助画像与场景命中，不改动话术原文字符 |
| 失败边界 | `extract_selling_point_fields()`：无该 `product_id` 行或 `product_name` 为空 → `None`（工厂返回 `None` → `code: "2"`）；有行但话术全空 → 只输出【卖点】段 |
| 字段 dict | `product_id / business_name / auxiliary_portrait / cut_in_statement / selling_point_block / retain_statement`，不含任何诊断 8 表字段、不含 `is_fallback` / `fallback_reason` |

**【辅助画像】的硬编码标签 ID 开关（`services/selling_point_script/logic.py`）**：`_LABEL_ZERO_TO_WU`（`0.00→无`）、`_LABEL_FEE_YUAN`（费用加「元」）、`_LABEL_FLOW_M`（流量加「M」，2 个 ID）、`_LABEL_BOOL`（`1 / 0 → 是 / 否`）、`_LABEL_DOWNGRADE_RED`（降档阈值名称标红，2 个 ID）共 5 组常量，取值在代码内逐项固化，转换只作用于命中的 ITEMCODE。属外部依赖：上游标签 ITEMCODE 的获取渠道 <待填写：上游标签字典 / 接口人>，责任人 <待填写：配置管理员>；维表「标签ID」子集未命中这些 ID 时对应转换不生效，画像按 IOP 原值输出。

**维表列声明（`db_config.py`）**：`SELLING_POINTS_REQUIRED_COLUMNS = (product_id, label_id)`；`SELLING_POINTS_OPTIONAL_COLUMNS = (product_name, selling_point, fallback_script, match_result)`；`SELLING_POINTS_TAG_COLUMNS = tag_0 … tag_27`（28 个）；`SELLING_POINTS_SELECT_COLUMNS` 共 34 列，按「必需 → 可选 → tag」顺序 SELECT。`SELLING_POINTS_SCHEMA` 另含 `product_category / product_subcategory`，仅用于离线自检建临时库。`dimensions.py` 中 `load_selling_points()` 按 500 个 ID 分块、`mode=ro` 只读、`split_label_ids()` 正则 `[\n\r|｜,，;；]+` → 去空、保序、去重；`configure(None)` 还原默认库路径。另声明 `LABEL_NAME_MAPPING_TABLE = label_name_mapping` / `LABEL_NAME_MAPPING_COLUMNS = (label_id, display_name)`：`load_label_name_mapping()` 用 `mode=ro` 全表读一次，返回 `{label_id: 营销助手显示名字}`，表缺失 / 列漂移 / 异常降级 `{}`。

## 7. IOP / 画像 / 卖点：由 `main.py` 注入、工厂按业务分发

本层不 import `IOP`。请求内编排在 `main.py`：

1. `need_labels(product_id)`（本层 `routing.py`）决定该 ID 是否查标签：商机回捞、诊断链路与卖点话术模块都要用，控损不用，故除控损外一律返回 `True`。诊断由 `F09629` 动态命中、没有静态 ID 白名单，「会不会落到卖点话术」也要等诊断查完 8 表才知道，在 `product_id` 级别无法预判；请求级提供者保证整请求最多查 1 次 IOP。
2. `IopLabelProvider` 在请求内惰性取一次标签，任何失败降级 `{}`；同请求商机 / 诊断 / 卖点话术共用这一份，最多查 1 次 IOP。
3. 循环外读两次 `dimensions.db`（各 1 次 `mode=ro` 连接、无进程级缓存）：`load_selling_points(所有 productId)` 得 `{product_id: SellingPointRow}`，`load_label_name_mapping()` 得 `{label_id: 显示名}`。两者与 `custom_label_info` 一起传给 `build_product_object()`；工厂按分支只转发：商机对象收标签与维表行并交给 `AuxiliaryPortrait`，诊断取数**只收标签**（不收维表行），控损只收 `record`，**卖点话术收维表行 + 标签 + 显示名映射**，兜底只收维表行用于日志。

只有商机对象持有 `AuxiliaryPortrait` 组件（组合 + 懒执行 + 实例内缓存）；卖点话术不持有该组件，而是在 `extract_selling_point_fields()` 里用维表 `label_ids` + IOP 返回 + `label_name_mapping` 一次性算出【辅助画像】字符串，放进 record 由 `build_output_script()` 拼出。控损 / 诊断 / 兜底都不拼画像。维表未命中 / 库缺失 / IOP 降级 → 商机与卖点话术的画像都为空、场景不命中；诊断侧是占位符原样保留。

## 8. 降级速查

| 触发 | 行为 |
|---|---|
| 业务库缺失 / 查询异常 | `reader` 返回 `{}` / `[]`；商机 / 控损按空记录出话术（仍 `code: "0"`），诊断返回 `None` → 卖点话术模块，API 不 500 |
| 分表 AVL 的 `where` 缺标量 `product_no` | `reader` 返回 `[]`（不广播、不抛异常） |
| `F09633` 分表数改动但未重灌全量 | 号码落到其它分表查不到 → 诊断返回 `None` → 卖点话术模块 |
| 诊断 7 个查询任一为空 / 交集为空 / 六项全空 | 诊断返回 `None`；路由改问平级的卖点话术模块，按上一版格式输出（辅助画像 / 切入 / 卖点 / 挽留），`code: "0"` |
| 维表里没有该 `product_id`，或该行产品名称为空 | 卖点话术模块返回 `None` → 落 `ProductInfoRaw`（`code: "2"`） |
| 卖点话术原文为空 /「匹配结果2」为空 | 「匹配结果2」为空时仅当「兜底话术」为空且「标签0」非空才回退「标签0」原文输出兜底组，其余情况切入 / 挽留段整段不出现、只留【卖点】；不填「无」，不回退其它「标签N」 |
| IOP 标签失败 | `main.py` 降级 `{}` 注入；商机画像为空，诊断三类话术里的 `#标签名&` 原样保留；卖点话术辅助画像为空、场景不命中 → 用「兜底话术」，输出结构不变 |
| 卖点维表库缺失 / 表或列漂移 / 查询异常 | `dimensions` 降级 `{}`，商机辅助画像为空，卖点话术无行可用 → `code: "2"`，其余话术段照常 |
| `product_id` 未命中维表 / 「标签ID」为空 | 标签子集为空、辅助画像为空串，不 500 |
| 维表写入侧故障 | 保留上次成功版本，在线侧下次读到的仍是它；不会让 API 500 |

## 9. 话术 HTML 精确模板与解析规则

本节是逐字符规格：四类业务对象的 `output_script` 由各自的 `rewrite.py` / `logic.py` 拼出，库里只存原始文本，所有 HTML 都在这里生成。诊断与卖点话术的模板刻意各自实现，互不复用，方便后者独立改组合。

**公共形状（`services/product_info_raw.py`）**

`ProductInfoBase(product_id, product_name=None, output_script="无")`；`__str__` 输出 `Product ID / Product Name / 子类 _extra_str_lines() / output_script` 四部分日志行。`ProductInfoRaw`（第 5 类兜底）的 `product_name` 取维表行的 `product_name`（可能非空）、`output_script="无"`；但 `main.py` 对非业务对象**强制**输出 `{"productName": "", "script": "", "code": "2"}`，所以 `code` 只能按对象类型判定。

`build_auxiliary_portrait(queried_label_nvs)`：逐项 `f'{label_name}: <strong><span style="color:blue;">{label_value}</span></strong>; '`，跳过 `None` 项与空 `label_value`；非空时前置 `【辅助画像】` 并 `rstrip('; ')`；空输入返回空串，商机话术不追加该段。

**商机回捞（`opportunity_recall/rewrite.py`）**

`product_name` 由 `PRODUCT_NAMES` 静态映射：`202608041615335173` → `号卡业务回捞商机`、`202608041615335172` → `套餐业务回捞商机`、`202608041615335174` → `宽带业务回捞商机`。四段话术实际由 6 行 HTML 片段组成（空值一律 `无`）：

```python
lines = [
  f'<strong>【通话总结】</strong>{rec.get("content_abstract") or "无"}',
  '<strong>【用户需求】</strong>'
  f'<strong><span style="color:blue;">{rec.get("type_xl") or "无"}：</span></strong>'
  f'{rec.get("cust_demand") or "无"}',
  f'<strong>【已推荐政策】</strong>{rec.get("rec_strategy") or "无"}',
  '<strong>【拒绝原因】</strong>',
  '<strong><span style="color:blue;">拒绝原因：</span></strong>'
  f'{rec.get("reject_reason") or "无"}',
  '<strong><span style="color:blue;">具体表述：</span></strong>'
  f'{rec.get("reason_note") or "无"}',
]
if self.auxiliary_portrait:      # 非空才追加
    lines.append(self.auxiliary_portrait)
return "\n".join(lines).strip().replace("\n", "<br>")
```

即「用户需求」段是 `【用户需求】` 标题 + 蓝色 `type_xl：` + `cust_demand`；「拒绝原因」段是标题行 + 两条蓝色子项（`拒绝原因：` / `具体表述：`）。消费的业务库列只有 `content_abstract / type_xl / cust_demand / rec_strategy / reject_reason / reason_note`。

**控损（`loss_control/rewrite.py`）**

`product_name="控损业务专用"`；`record` 无 `strategy_name` → 输出「无历史话术记录」；有值 → `parse_custom_tags(raw).strip().replace("\n", "<br>")`。

`parse_custom_tags()` 是模块级纯函数，按「就近取标签」扫描，正则为 `<\$([^>]+)>`、`<#([0-9A-Fa-f]{6})>`、`</>`，另有内嵌色码 `#([0-9A-Fa-f]{6})`：

| 命中 | 输出 | 压栈 |
|---|---|---|
| `<$...>` | 内容含 `B` / `I` / `U` 时按此顺序追加 `<b>` / `<i>` / `<u>` | 依次压 `b` / `i` / `u` |
| `<$...>` 且内容含 `#RRGGBB` | 追加 `<span style="color: #RRGGBB">` | 压 `span` |
| 独立 `<#RRGGBB>` | 追加 `<span style="color: #RRGGBB">` | 压 `span` |
| `</>` | `while` 弹空整个栈：`span` → `</span>`，其余 → `</b>` / `</i>` / `</u>`（逆序闭合） | 清空 |

标签之外的文本原样 append；flag 判定是**子串** `in content`，因此 `<$B#0070C0>` 这类组合标签会同时产出加粗与颜色。样例 `F0959120261110000000.AVL` 只覆盖 `<$B>`、3 种颜色与 `</>`（26 个），`<$I>` / `<$U>` 与组合标签未覆盖但代码支持。

**诊断（`diagnosis/rewrite.py` + `diagnosis/logic.py`）**

`ProductInfoByDiagnosis` 令 `product_name = record.get("business_name") or ""`（只可能是 `F09629.BUSINESS_NAME` 原文），`output_script = build_output_script(record)`，固定六段、不传 `omit`。六段的 `line()` 模板、多行 `<br>` 拼接、挽留蓝色子项见第 6.3 节。

**卖点维表话术（`selling_point_script/logic.py`）**

`ProductInfoBySellingPoint` 令 `product_name = record.get("business_name") or ""`（维表「产品名称」原文），`output_script = build_output_script(record)`，逐字符复现上一版 `ProductInfoRewrite` 的 HTML 形状，唯一差异是末尾的换行转换：

```python
output_script = ""
output_script += (record["auxiliary_portrait"] + "\n") if ... else ""
output_script += (record["cut_in_statement"] + "\n") if ... else ""
output_script += (record["selling_point_block"] + "\n") if ... else ""
output_script += record["retain_statement"] if ... else ""
output_script = strong_sections(output_script)   # 【xxx】 → <strong>【xxx】</strong>
output_script = output_script.replace("\n", "<br>")  # 与商机 / 控损一致
```

样例（无标签数据时；按行展示便于阅读，实际 `output_script` 中这些位置均为 `<br>`）：

```
<strong>【切入话术】</strong>
<strong><span style="color: blue;">基于客户标签特征切入（壹号）：</span></strong>切入B
<strong>【卖点】</strong>
<strong>1、资费：<span style="color:red;">基础100分钟+1GB国内。</span></strong>
<strong>2、价格：</strong>9元/月。
<strong>3、业务确认：</strong>合约期1年。
<strong>【挽留话术】</strong>
<strong><span style="color: blue;">担心合约：</span></strong>挽留B
```

段间与段内的换行在出口统一为 `<br>`（不再含真实 `\n`）、空段整段不出现（不填「无」）、最后一段无尾换行；没有 `omit` 分支、没有标签替换、没有资费名称 / 摘要 / 痛点 / 营销段。`color: blue;` 带空格、`color:red;` 不带，均为当前固定写法。

## 10. 自检

```bash
python tests/check_diagnosis_offline.py      # 零第三方依赖主体，不连 SFTP / IOP
python -m call_stat --self-check            # 调用量统计组件自检
```

`check_diagnosis_offline.py` 用 8 个样例 AVL 建临时库并补一组业务键自洽数据；其中卖点话术部分把上一版 `ProductInfoRewrite.output_script` 的逐字符期望固化成 `EXPECTED_LEGACY_OUTPUT`（无 IOP 时），离线自检与在线路由两处都断言相等；另用独立的 `tests/legacy_output_reference.py` 直译上一版四个历史文件，对「有 IOP 返回 + `label_name_mapping` + 标签专属话术命中」的完整输出做 golden 逐字符比对；`tests/check_legacy_output_parity.py` 把同一 oracle 跑在真实 `dimensions.db` 全量（1361 个 product_id × 多组 IOP 返回 = 5121 次输出）上，逐字符不一致 0。断言：注册 / 解析 / 入库、单一 flat 解析器、`F09633` 分表（128 张表、号码不串表、按 telnum 精确定位、缺 `product_no` 降级 `[]`、两侧分表口径一致）、卖点维表 → 商机标签 ID（按 `db_config` 声明建临时 `dimensions.db`、换行 / 竖线拆分、库缺失降级）、**卖点话术格式复现**（`cut_in_str2tuple` / `build_selling_point` / `strong_sections` 逐字符断言、`EXPECTED_LEGACY_OUTPUT` 端到端相等、出口 `\n` → `<br>` 且不再含真实换行、「匹配结果2」为空不输出切入挽留、按标签选话术可选路径、无该行或产品名为空返回 `None`、`#标签名&` 原样保留）、多行查询、诊断 7 表关联与六段话术、诊断不适用 → 平级卖点话术对象、业务库未就绪时商机 / 控损按空记录出话术、商机 / 诊断共用请求级 IOP 标签（整请求 1 次）、`F09635` 主键 `tag_name` 与三类话术 `#标签名&` → `ITEMVALUE` 替换（含降级保留）、在线侧 `code=0` 且与上一版输出逐字符一致，结论 `ALL PASS`。

脚本里写入侧配置（`load_profiles(runtime_dir=TMP)`）与在线侧配置（`db_config.load(avl_db_root=TMP/sqlite_db, ...)`）各自独立装配，共同指向同一批临时 db 文件；临时 `sftp_avl2db/.env` 只服务写入侧，结束后删除；维表库用临时路径注入，不写包内 `runtime/`。

## 11. 新增一类业务

1. 写入侧注册 AVL / 表结构（见 `sftp_avl2db/README.md`）；同时在 `services/db_config.py` 补在线侧声明：`TABLE_SCHEMAS` 加列顺序、`AVL_DB_SPECS` 加 `db_file` / 查询键 / 排序 / 分表（分表数还要写进 `.env` 与 `.env.example`）。两侧各自维护，但列顺序与分表口径必须一致。
2. 在 `services/` 下按业务名加一个包，`rewrite.py` 里继承 `ProductInfoBase` 实现话术重组，构造只收本业务真正消费的参数；需要辅助画像才组合 `AuxiliaryPortrait`（商机回捞、诊断链路与卖点话术在 `need_labels()` 里声明查 IOP，控损不查；只消费维表原文、不做字段替换的业务不需要收 `custom_label_info`；需要复现上一版辅助画像的业务照 `selling_point_script/` 的写法收 IOP 返回与 `label_name_mapping`）。`__init__.py` 显式导出语义化类名。
3. `routing.py`：加静态映射（若需要）、在 `build_product_object()` 里按序插入分支、把新对象类加进 `BUSINESS_OBJECT_TYPES`。
4. `services/__init__.py` 加一条延迟导出。
5. 若新业务需要标签子集，在卖点维表 `selling_points` 里给对应 `product_id` 配好「标签ID」列；若也要卖点话术，再配「产品名称」「生成话术」「兜底话术」「匹配结果2」与「标签N」原文。
6. 更新根 README 与本文件的路由表、`tests/check_diagnosis_offline.py` 的断言。
