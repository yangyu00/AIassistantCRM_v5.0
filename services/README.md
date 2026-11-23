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
| 公共基类 / 画像组件 | `product_info_raw.py` | `ProductInfoBase`（最小公共形状）、`AuxiliaryPortrait`（商机专属，组合）、`ProductInfoRaw`（第 4 类兜底对象）、`build_auxiliary_portrait()` |
| 业务库读取侧 | `reader.py` | `query` / `query_many` / `query_in`；只认 AVL 前缀 |
| 卖点维表读取侧 | `dimensions.py` | `load_selling_points()` / `split_label_ids()`；`product_id → label_ids / product_name / fallback_script / tag_*` |
| 路由 | `routing.py` | `product_id → AVL` 两张静态表、`need_labels()`、`build_product_object()` |
| 商机回捞 | `opportunity_recall/` | 四段话术 + 辅助画像 |
| 控损 | `loss_control/` | `strategy_name` 自定义标签 → HTML；`parse_custom_tags()` 为模块级纯函数 |
| 诊断 | `diagnosis/` | 取数、纯逻辑、六段话术对象、口径常量、卖点维表兜底 |

三类业务包以业务名命名，包内 `rewrite.py` 承载业务对象、`__init__.py` 显式导出。

**对象模型**：四类对象只继承极简 `ProductInfoBase`（`product_id` / `product_name` / `output_script` + 统一日志头）；辅助画像用组合挂在商机对象的 `AuxiliaryPortrait` 上，结果只缓存在该组件实例上。控损 / 诊断 / 兜底对象不持有画像、标签、维表属性。基类通过 `_extra_str_lines()` 钩子统一拼装公共日志行。

| 业务 | 对外 API | 定义位置 |
|---|---|---|
| 商机回捞 | `ProductInfoByOpportunityRecall` | `opportunity_recall/rewrite.py` |
| 控损 | `ProductInfoByLossControl` | `loss_control/rewrite.py` |
| 诊断 | `ProductInfoByDiagnosis` | `diagnosis/rewrite.py` |

## 3. 四类路由（顺序不可调换）

`build_product_object(product_id, telnum, *, custom_label_info, selling_points)` 是唯一对象工厂；`main.py` 用 `isinstance(obj, BUSINESS_OBJECT_TYPES)` 判 `code`（`"0"` 带话术 / `"2"` 空话术）。

| 顺序 | 条件 | 数据源 | 对象 |
|---|---|---|---|
| 1 | 商机回捞 3 个 ID：`202608041615335173/172/174` → `F09554/F09555/F09556` | `reader.query(avl, telnum)` | `ProductInfoByOpportunityRecall` |
| 2 | 控损 1 个 ID：`202609180955095402` → `F09591`，命中即返回 | `reader.query(avl, telnum)` | `ProductInfoByLossControl` |
| 3 | 诊断 | `diagnosis.build_diagnosis_record()`（7 表关联）；任一环节失败改用卖点维表兜底 | `ProductInfoByDiagnosis` |
| 4 | 其余 | — | `ProductInfoRaw`（兜底） |

- 业务库未就绪（`reader.query` 返回 `{}`）时按空记录出话术：商机仍输出四段（各段填「无」），控损输出「无历史话术记录」，都不落到诊断 / 兜底，也不抛 500。
- 诊断不维护静态映射：`F09629`（`DIM_MARKET_PRODUCT_BUSINESS_ONCE`）本身即「产品ID → 策略 / 产品」映射表。7 表关联任一环节失败时用卖点维表兜底，仍返回 `ProductInfoByDiagnosis`（`code: "0"`）；只有维表里没有该 `product_id`，**或有该行但 `product_name` 为空**时，才落第 4 类 `code: "2"`（边界见第 6 节 `fallback.py`）。
- `product_id → AVL 前缀` 的两张静态表在 `routing.py` 唯一维护；`reader` 只按 `db_config` 声明的 `{project}/{AVL}.db` 定位。
- `selling_points` 由 `main.py` 在循环外调一次 `dimensions.load_selling_points()` 批量查回，只透传给对象工厂，不参与路由判定。

## 4. 业务库读取侧：`reader.py` + `db_config.py`

`db_config.py` 独立维护：

- `AVL_DB_SPECS`：12 个 AVL 各自的 `db_file`（如 `diagnosis/F09633.db`）、schema 名、单条查询键 `where`、固定 `order_by`、是否分表。
- `TABLE_SCHEMAS` / `TABLE_PRIMARY_KEYS`：各表列顺序 / 类型（与写入侧建表 DDL 对齐）与主键；`SELECT *` 回来的 dict 按此列顺序组装。
- `services/.env`：共 5 键——`AVL_DB_ROOT`、`DIMENSIONS_DB`、`LOSS_CONTROL_NUM_TABLES`、`F09633_NUM_TABLES`、`AVL_FRESHNESS_WINDOW_DAYS`（默认 `2`，`0` 关闭新鲜度过滤，见第 4.1 节）；缺文件 / 缺键用 `db_config.py` 顶部代码内默认值（默认指向 `sftp_avl2db/runtime/sqlite_db` 等）。与根 README 第 4 节的 5 键口径一致。
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

`INSERT OR REPLACE` 只覆盖「本批出现过的主键」，上游不再下发的主键会带着旧 `op_time` 留在库里。在线侧用一个变量把这些过时行挡在查询外：

| 项 | 位置 | 说明 |
|---|---|---|
| 批次水位 | 写入侧在每个 `{AVL}.db` 内维护的 `_avl_meta` 表 | 一行 / AVL：`latest_file_date`（文件名日期）、`latest_op_time`（该批数据最大 `op_time`）、`last_filename`、`updated_at`；只在「新文件日期 ≥ 已记录日期」时推进，补跑旧文件不回退 |
| 变量 `AVL_FRESHNESS_WINDOW_DAYS` | `services/.env`（默认 `2`，`0` 关闭） | `N` = 只召回 `op_time ∈ [最新批次-(N-1)天, 最新批次]` 的数据。以数据里的 `op_time` 为基准，不依赖「`op_time` = 文件日期 - 1」这一当前事实 |

- **单条** `query()`：`op_time >= 下界` + `ORDER BY op_time DESC LIMIT 1`（窗口内取最新一条）。
- **多条** `query_many()` / `query_in()`：先按窗口过滤，再在**结果集内**收敛到 `op_time = MAX(op_time)` 的那一批。例：最新批次 21 日、`N=2`（下界 20 日），结果里同时有 20 / 21 日 → 只返回 21 日集合。
- 下界由 `_cutoff()` 按 `YYYY-MM-DD` 计算；水位表缺失 / 无行 / 日期格式异常 / `N=0` 一律降级为不过滤。
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

`main.py` 每请求调一次 `load_selling_points(product_ids)`，按本次全部 `productId` 批量 `IN` 查 `selling_points`，返回 `{product_id: SellingPointRow}`。

| 项 | 口径（均在 `db_config.py` 声明） |
|---|---|
| 库 | `DIMENSIONS_DB`（默认 `local_excel2db/runtime/sqlite_db/dimensions.db`） |
| 连接 | `sqlite3` + `mode=ro` 只读 |
| 表 / 列名 | `SELLING_POINTS_TABLE` = `selling_points`；`SELLING_POINTS_SELECT_COLUMNS` 为线上实际 SELECT 的 34 列（2 必需 + 4 可选 + `tag_0 … tag_27`）。`SELLING_POINTS_SCHEMA` 多出 `product_category` / `product_subcategory`，仅用于离线自检建临时库 |
| 商机画像用列 | `product_id`（主键）→ `label_id`（Excel「标签ID」） |
| 诊断兜底用列 | `product_name`（产品名称）、`fallback_script`（「兜底话术」原文）、`tag_0 … tag_27`（「标签N」原文，作回退） |
| 其它列 | `selling_point` / `match_result` 保留原文本；可选列缺失填空串 |
| 只存原始文本 | 库里所有话术 / 标签列都是 `TEXT` 原文；`split_label_ids()`、`parse_tag_script()`、`split_fee_name()` 等加工都在读取侧 |

`SellingPointRow` 只把「标签ID」拆成 `label_ids`；`fallback_script` / `tag_texts` / `match_result` 都是库里的原始文本。

- 每请求 1 次、不做进程级缓存：写入侧按固定间隔整表覆盖，缓存会让新维表不生效；同一请求内靠返回的 dict 复用，重复 ID 只查一次。
- 列名走 `db_config` 白名单、值一律参数绑定，IN 列表按 500 分块。
- `split_label_ids()`：换行 / 竖线 / 逗号 / 分号（含全角）→ 去空、保序、去重。
- 库不存在 / 表缺失 / 必需列漂移 / 查询异常 → 打日志并降级 `{}`；未命中的 `product_id` 不在返回 dict 中。商机辅助画像为空，诊断无法兜底（维持 `code: "2"`），不抛给在线接口。
- 离线打桩：`dimensions.configure(临时 dimensions.db)`，传 `None` 还原默认路径。

分工：商机侧由维表 `label_id` 决定「查哪些 ID」、IOP 返回的 `custom_label_info` 决定「名称与值」；诊断兜底侧不查 IOP，只取维表「兜底话术」原文（为空时回退第一个非空「标签N」原文）解析成话术。写入侧配置见 [`local_excel2db/README.md`](../local_excel2db/README.md)，在线侧列映射不取自它。

## 6. 诊断（`services/diagnosis/`）

8 个 AVL（`F09627`～`F09633` / `F09635`）在 `db_config.AVL_DB_SPECS` 里各有一份在线侧声明；主链路用其中 7 张表，`F09635`（标签ID-标签名）不进链路。`F09633` 是唯一带 `product_no` 的表，按手机号分 128 张表。

| 文件 | 职责 |
|---|---|
| `constants.py` | 8 个 AVL 前缀、多值分隔符、括号正则、RN 方向、切入话术降级开关 |
| `logic.py` | 纯函数零 I/O：括号拆分、多值拆分、按位置配对、场景交集、优先级排序、HTML 拼装（`build_output_script(..., omit=())` 可整段省略） |
| `query.py` | `build_diagnosis_record(product_id, telnum, *, selling_points)`，async，7 表关联 + 兜底触发，不查 IOP |
| `fallback.py` | 卖点维表兜底：`pick_script_text()` / `parse_tag_script()` / `build_fallback_record()`，纯函数零 I/O |
| `rewrite.py` | `ProductInfoByDiagnosis`，把 record 拼成 `output_script`；兜底 record 省略两段 |

### 7 表关联取数顺序

1. `F09629` 按 `product_id` 取 `BUSINESS_NAME` / `BUSINESS_TYPE`；`BUSINESS_NAME` 按【】内外拆成资费名称 / 资费摘要。
2. `F09628` 按 `business_type` 取产品场景集合（多行）。
3. `F09633` 按 `product_no`（标量，同时是分表键）取用户场景与痛点诊断，单元格多值按位置配对。
4. 用户场景 ∩ 产品场景，保留全部交集场景的 `REASON` 作为痛点诊断。
5. 交集场景按 `F09627` 的 RN 排优先级（默认 RN 越小优先级越高），取第一个能取到 `F09630` 切入话术的场景；`ALLOW_ENTRY_SCRIPT_FALLBACK` 开启时允许无 RN 的交集场景按原顺序补位。
6. `F09631` 按 `business_type` 取营销话术（多行，去重）。
7. `F09632` 取 `reject_reason` / `wl_huashu`，多值按位置配对成挽留话术列表。

正常链路输出六段固定：`【资费名称 / 资费摘要 / 痛点诊断 / 切入话术 / 营销话术 / 挽留话术】`，空值填「无」。

### 卖点维表兜底

触发（`query.py` 统一出口 `fallback(reason)`）：

| 条件 | `fallback_reason` |
|---|---|
| 7 个查询任一返回空（或提取出的有效值为空） | `db_empty:{AVL}` |
| 场景交集为空 | `empty_intersection` |
| 六项输出全空 | `empty_output` |

兜底内容（来源都是 `selling_points` 该行）：

1. `product_name` 用 `split_fee_name()` 按【】拆成资费名称 / 资费摘要；响应 `productName` 只取 `record['business_name']` 原文（此处即维表「产品名称」）。
2. 取 `fallback_script`（「兜底话术」）原文，为空时回退到「标签N」列第一个非空原文。
3. `parse_tag_script()` 按 `切入话术` / `拒绝挽留话术` 两个标题分段：前者收集非空行用 `<br>` 拼接；后者每行按首个全角 / 半角冒号拆成 `(拒绝原因, 挽留话术)`；解析时去掉 `**` 加粗标记。
4. 痛点诊断与营销话术没有维表来源，由 `FALLBACK_OMIT_SECTIONS` 控制整段不输出（不是填「无」），最终四段：资费名称 / 资费摘要 / 切入话术 / 挽留话术。

降级边界：维表里没有该 `product_id` 或产品名称为空 → `build_fallback_record()` 返回 `None` → 上层落 `code: "2"`；有行但话术原文全空 → 仍输出资费名称 / 摘要，切入与挽留段填「无」。`fallback_reason` 写进 record 与 `__str__`，便于日志排查。

### 实现级口径（`constants.py` / `logic.py` / `query.py` / `fallback.py`）

仅靠上面的概要无法复现逐字符行为，下面是代码里的精确口径。

**常量（`services/diagnosis/constants.py`）**

| 项 | 值 |
|---|---|
| 8 个 AVL 前缀 | `F09627` 场景优先级、`F09628` 产品-场景、`F09629` 产品策略、`F09630` 场景-切入话术、`F09631` 产品-营销话术、`F09632` 产品-挽留话术、`F09633` 用户场景（分表）、`F09635` 标签ID-标签名（不进链路） |
| `SMALLER_RN_HIGHER_PRIORITY` | `True`（RN 越小优先级越高） |
| `ALLOW_ENTRY_SCRIPT_FALLBACK` | `True`（允许无 RN 的交集场景按原顺序补位） |
| `EMPTY_TEXT` | `无` |

`FALLBACK_OMIT_SECTIONS = ("痛点诊断", "营销话术")` **不在 `constants.py`**：它定义在 `services/diagnosis/fallback.py`，由 `diagnosis/__init__.py` 从 `fallback` 延迟导出；`ProductInfoByDiagnosis` 在兜底 record 时用它把两段整段省略（不是填「无」）。要改兜底段落请改 `fallback.py`。

两条正则本身含竖线，放在表格外以免与 Markdown 列分隔符冲突：

- `MULTI_VALUE_SPLIT_RE` = `[|｜\n]+`（竖线 / 全角竖线 / 单元格内换行；**不含逗号、分号**）
- `FEE_BRACKET_RE` = `【([^】]*)】|\[([^\[\]]*)\]`（同时支持 ASCII `[]`）

**纯函数口径（`logic.py`）**

- `split_fee_name(text)`：`strip()` 后无括号 → `("", 全文)`；有括号 → 名称取 `group(1) or group(2)`，摘要为**用该正则删掉全部括号**后的剩余文本再 `strip()`。样例 `【五大重点客群】不限量客户10元享40G阈值扩容` → `("五大重点客群", "不限量客户10元享40G阈值扩容")`。
- `split_multi()` → 去空白、去空项；`pairs_by_position()` 用 `zip_longest(..., fillvalue="")` 按相同下标配对。
- `dedup()` 按首次出现顺序去重、跳过空值；`extract_scripts(rows, column)` 取列、`strip()` 后 `dedup`。
- `expand_user_scenes(rows)`：只收 `scene` 非空的 `(scene, reason)`；`intersect_scenes()` 保留交集首次出现顺序，同一场景可贡献多条非空 `REASON` 作为痛点诊断。
- `order_scenes_by_priority(scenes, rank_rows)`：`rn` 只能 `int()` 成功才入 ranks；排序键有 RN 为 `(0, rn, index)`、无 RN 为 `(1, 0, index)`；`allow_fallback=False` 时才过滤掉无 RN 场景。返回 `(ordered, ranked_set)`。
- `expand_retain_pairs(rows)`：按位置配对后**只收 `huashu` 非空**的 `(拒绝原因, 挽留话术)`。
- `build_output_script(record, omit=())`：每段 `line(label, value)` = `<strong>【{label}】</strong>{value or 无}`；痛点 / 营销为多行 `<br>` 拼接；挽留每行 `<strong><span style="color:blue;">{reason or 无}：</span></strong>{huashu or 无}`；六段按固定顺序 `<br>` 连接，`omit` 中的段整段不出现。

**7 表关联的精确调用（`query.py`）**

| 步 | 调用 | 空结果处理 |
|---|---|---|
| 1 | `query_many(F09629, where={"product_id": product_id}, limit=1)` | `db_empty:F09629` |
| 2 | `query_many(F09628, where={"business_type": business_type})` → `dedup(scene)` | `db_empty:F09628` |
| 3 | `query_many(F09633, where={"product_no": telnum})` → `expand_user_scenes` | `db_empty:F09633` |
| 4 | `intersect_scenes(user_pairs, product_scenes)` | 交集为空 → `empty_intersection` |
| 5 | `query_in(F09627, "scene", matched_scenes)` → `order_scenes_by_priority` → `query_in(F09630, "scene", ordered_scenes)`；`qr_map` 只收 `qr_huashu` 非空；`top_scene` 取 `ordered_scenes` 里第一个能查到话术的场景，`top_scene_source` ∈ `rank` / `fallback` / `none` | 任一查询结果为空 → `db_empty:F09627` / `db_empty:F09630` |
| 6 | `query_many(F09631, where={"business_type": business_type})` → `extract_scripts` | `db_empty:F09631` |
| 7 | `query_many(F09632, where={"business_type": business_type})` → `expand_retain_pairs` | `db_empty:F09632` |

record 字段固定为 `product_id / telnum / business_name / business_type / fee_name / fee_summary / product_scenes / user_scenes / matched_scenes / pain_points / top_scene / top_scene_source / entry_script / marketing_scripts / retain_pairs`。六项输出 `fee_name, fee_summary, pain_points, entry_script, marketing_scripts, retain_pairs` 全空时 → `empty_output` 兜底。

**兜底话术原文解析（`fallback.py`）**

| 项 | 值 |
|---|---|
| 触发原因 | `db_empty:{AVL}` / `empty_intersection` / `empty_output`（写进 record 与 `__str__`） |
| 段落标题 | `切入话术`、`拒绝挽留话术`（判定用 `rstrip("：: ")` 后精确相等） |
| 行清洗 | `_clean_line()`：`strip()` → 去掉全部 `**` → 再 `strip()` |
| 噪声行 | 以 `Traceback` 或 `File "` 开头的行跳过；空行跳过 |
| 切入段 | 标题后的非空行收集，`<br>` 拼接 |
| 挽留段 | 每行按**首个**全角 / 半角冒号 `[：:]` 拆成 `(拒绝原因, 挽留话术)`；无冒号 → `("", 整行)` |
| 原文优先级 | `pick_script_text()`：`fallback_script.strip()` → 否则 `tag_texts`（`tag_0…tag_27`）里第一个非空原文 |
| 失败边界 | `build_fallback_record()`：无该 `product_id` 行或 `product_name` 为空 → `None`（上层 `code: "2"`）；有行但话术全空 → 仍输出四段，切入 / 挽留填「无」 |
| 兜底 record | `is_fallback=True`、`business_name` 取维表「产品名称」原文、`pain_points=[]`、`marketing_scripts=[]` |

**维表列声明（`db_config.py`）**：`SELLING_POINTS_REQUIRED_COLUMNS = (product_id, label_id)`；`SELLING_POINTS_OPTIONAL_COLUMNS = (product_name, selling_point, fallback_script, match_result)`；`SELLING_POINTS_TAG_COLUMNS = tag_0 … tag_27`（28 个）；`SELLING_POINTS_SELECT_COLUMNS` 共 34 列，按「必需 → 可选 → tag」顺序 SELECT。`SELLING_POINTS_SCHEMA` 另含 `product_category / product_subcategory`，仅用于离线自检建临时库。`dimensions.py` 中 `load_selling_points()` 按 500 个 ID 分块、`mode=ro` 只读、`split_label_ids()` 正则 `[\n\r|｜,，;；]+` → 去空、保序、去重；`configure(None)` 还原默认库路径。

## 7. IOP / 画像 / 卖点：由 `main.py` 注入、工厂按业务分发

本层不 import `IOP`。请求内编排在 `main.py`：

1. `need_labels(product_id)`（本层 `routing.py`）决定该 ID 是否查标签，只有商机回捞 3 个 ID 返回 `True`。
2. `IopLabelProvider` 在请求内惰性取一次标签，任何失败降级 `{}`。
3. 循环外调一次 `dimensions.load_selling_points(所有 productId)`，把 `{product_id: SellingPointRow}` 作为 `selling_points` 传给 `build_product_object()`；工厂按分支只转发：商机对象收标签与维表行并交给 `AuxiliaryPortrait`，诊断取数只收维表行，控损只收 `record`，兜底只收维表行用于日志。

只有商机对象持有 `AuxiliaryPortrait`，工厂不给其它分支传画像参数，编排层也不建请求级画像缓存。组件按维表行的 `label_ids` 对齐 `queried_label_ids` / `queried_label_nvs`（名称与值取自 `custom_label_info`），标签子集与画像都是懒执行；画像结果只缓存在当前组件实例上。维表未命中 / 库缺失 / IOP 降级 → 标签子集为空，`build_auxiliary_portrait()` 返回空串，商机话术不追加 `【辅助画像】`。

## 8. 降级速查

| 触发 | 行为 |
|---|---|
| 业务库缺失 / 查询异常 | `reader` 返回 `{}` / `[]`；商机 / 控损按空记录出话术（仍 `code: "0"`），诊断触发维表兜底，API 不 500 |
| 分表 AVL 的 `where` 缺标量 `product_no` | `reader` 返回 `[]`（不广播、不抛异常） |
| `F09633` 分表数改动但未重灌全量 | 号码落到其它分表查不到 → 走维表兜底 |
| 诊断 7 个查询任一为空 / 交集为空 / 六项全空 | `query.py` 走维表兜底，输出四段，`code: "0"` |
| 维表里没有该 `product_id`，或该行产品名称为空 | 兜底失败 → 落 `code: "2"` |
| 兜底话术原文为空 | 回退第一个非空「标签N」原文；都为空白切入 / 挽留段填「无」 |
| IOP 标签失败 | `main.py` 降级 `{}` 注入；只影响商机画像，诊断链路不查 IOP |
| 卖点维表库缺失 / 表或列漂移 / 查询异常 | `dimensions` 降级 `{}`，商机辅助画像为空，话术其余段照常 |
| `product_id` 未命中维表 / 「标签ID」为空 | 标签子集为空、辅助画像为空串，不 500 |
| 维表写入侧故障 | 保留上次成功版本，在线侧下次读到的仍是它；不会让 API 500 |

## 9. 话术 HTML 精确模板与解析规则

本节是逐字符规格：三类业务对象的 `output_script` 由 `services/*/rewrite.py` 与 `diagnosis/logic.py` 拼出，库里只存原始文本，所有 HTML 都在这里生成。

**公共形状（`services/product_info_raw.py`）**

`ProductInfoBase(product_id, product_name=None, output_script="无")`；`__str__` 输出 `Product ID / Product Name / 子类 _extra_str_lines() / output_script` 四部分日志行。`ProductInfoRaw`（第 4 类兜底）的 `product_name` 取维表行的 `product_name`（可能非空）、`output_script="无"`；但 `main.py` 对非业务对象**强制**输出 `{"productName": "", "script": "", "code": "2"}`，所以 `code` 只能按对象类型判定。

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

`ProductInfoByDiagnosis` 令 `product_name = record.get("business_name") or ""`（正常链路是 `F09629.BUSINESS_NAME` 原文，兜底是维表「产品名称」原文），`output_script = build_output_script(record, omit=FALLBACK_OMIT_SECTIONS if is_fallback else ())`。六段 / 四段的 `line()` 模板、多行 `<br>` 拼接、挽留蓝色子项与三种 `fallback_reason` 见第 6 节「实现级口径」小节。

## 10. 自检

```bash
python tests/check_diagnosis_offline.py      # 零第三方依赖主体，不连 SFTP / IOP
python -m call_stat --self-check            # 调用量统计组件自检
```

`check_diagnosis_offline.py` 用 8 个样例 AVL 建临时库并补一组业务键自洽数据，断言：注册 / 解析 / 入库、单一 flat 解析器、`F09633` 分表（128 张表、号码不串表、按 telnum 精确定位、缺 `product_no` 降级 `[]`、两侧分表口径一致）、卖点维表 → 商机标签 ID（按 `db_config` 声明建临时 `dimensions.db`、换行 / 竖线拆分、库缺失降级）、诊断兜底（话术原文解析 /「兜底话术」优先 / db 查空、交集空、话术原文全空三种触发、四段输出且无痛点 / 营销、维表无该 ID 仍 `code: "2"`）、多行查询、7 表关联、六段话术、业务库未就绪时商机 / 控损按空记录出话术、只有商机回捞触发 IOP 查询、在线侧 `code=0` 四段兜底，结论 `ALL PASS`。

脚本里写入侧配置（`load_profiles(runtime_dir=TMP)`）与在线侧配置（`db_config.load(avl_db_root=TMP/sqlite_db, ...)`）各自独立装配，共同指向同一批临时 db 文件；临时 `sftp_avl2db/.env` 只服务写入侧，结束后删除；维表库用临时路径注入，不写包内 `runtime/`。

## 11. 新增一类业务

1. 写入侧注册 AVL / 表结构（见 `sftp_avl2db/README.md`）；同时在 `services/db_config.py` 补在线侧声明：`TABLE_SCHEMAS` 加列顺序、`AVL_DB_SPECS` 加 `db_file` / 查询键 / 排序 / 分表（分表数还要写进 `.env` 与 `.env.example`）。两侧各自维护，但列顺序与分表口径必须一致。
2. 在 `services/` 下按业务名加一个包，`rewrite.py` 里继承 `ProductInfoBase` 实现话术重组，构造只收本业务真正消费的参数；需要辅助画像才组合 `AuxiliaryPortrait`（只有商机回捞在 `need_labels()` 里声明查 IOP）。`__init__.py` 显式导出语义化类名。
3. `routing.py`：加静态映射（若需要）、在 `build_product_object()` 里按序插入分支、把新对象类加进 `BUSINESS_OBJECT_TYPES`。
4. `services/__init__.py` 加一条延迟导出。
5. 若新业务需要标签子集，在卖点维表 `selling_points` 里给对应 `product_id` 配好「标签ID」列；若也要诊断兜底，再配「产品名称」「兜底话术」与「标签N」原文。
6. 更新根 README 与本文件的路由表、`tests/check_diagnosis_offline.py` 的断言。
