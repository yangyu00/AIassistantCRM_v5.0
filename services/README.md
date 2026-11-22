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
- 诊断不维护静态映射：`F09629`（`DIM_MARKET_PRODUCT_BUSINESS_ONCE`）本身即「产品ID → 策略 / 产品」映射表。7 表关联任一环节失败时用卖点维表兜底，仍返回 `ProductInfoByDiagnosis`（`code: "0"`）；只有连维表也没有该 `product_id` 时才落第 4 类 `code: "2"`。
- `product_id → AVL 前缀` 的两张静态表在 `routing.py` 唯一维护；`reader` 只按 `db_config` 声明的 `{project}/{AVL}.db` 定位。
- `selling_points` 由 `main.py` 在循环外调一次 `dimensions.load_selling_points()` 批量查回，只透传给对象工厂，不参与路由判定。

## 4. 业务库读取侧：`reader.py` + `db_config.py`

`db_config.py` 独立维护：

- `AVL_DB_SPECS`：12 个 AVL 各自的 `db_file`（如 `diagnosis/F09633.db`）、schema 名、单条查询键 `where`、固定 `order_by`、是否分表。
- `TABLE_SCHEMAS` / `TABLE_PRIMARY_KEYS`：各表列顺序 / 类型（与写入侧建表 DDL 对齐）与主键；`SELECT *` 回来的 dict 按此列顺序组装。
- `services/.env`：`AVL_DB_ROOT`、`DIMENSIONS_DB`、`LOSS_CONTROL_NUM_TABLES`、`F09633_NUM_TABLES`；缺文件 / 缺键用 `db_config.py` 顶部代码内默认值（默认指向 `sftp_avl2db/runtime/sqlite_db` 等）。
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

两个分表 AVL：

| AVL | 业务 | 分表键 | 在线查询键 | 分表数（`db_config` 声明） |
|---|---|---|---|---|
| `F09591` | 控损 | `product_no` | `product_no`（手机号） | `LOSS_CONTROL_NUM_TABLES`（默认 128） |
| `F09633` | 诊断·用户场景对应关系 | `product_no` | `product_no`（`telnum`） | `F09633_NUM_TABLES`（默认 128） |

`F09633` 是诊断 8 表里唯一含手机号的表，千万级行；分表键与 `build_diagnosis_record()` 的 `where={"product_no": telnum}` 一致，一次请求只命中一张分表。写入侧按同一口径落库。

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

8 个 AVL（`F09627`～`F09633` / `F09635`）在 `db_config.AVL_DB_SPECS` 里各有一份在线侧声明；主链路用其中 7 张表，`F09635`（标签ID-标签名）只保留声明、不进链路。`F09633` 是唯一带 `product_no` 的表，按手机号分 128 张表。

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
| 维表里也没有该 `product_id` | 兜底失败 → 落 `code: "2"` |
| 兜底话术原文为空 | 回退第一个非空「标签N」原文；都为空白切入 / 挽留段填「无」 |
| IOP 标签失败 | `main.py` 降级 `{}` 注入；只影响商机画像，诊断链路不查 IOP |
| 卖点维表库缺失 / 表或列漂移 / 查询异常 | `dimensions` 降级 `{}`，商机辅助画像为空，话术其余段照常 |
| `product_id` 未命中维表 / 「标签ID」为空 | 标签子集为空、辅助画像为空串，不 500 |
| 维表写入侧故障 | 保留上次成功版本，在线侧下次读到的仍是它；不会让 API 500 |

## 9. 自检

```bash
python tests/check_diagnosis_offline.py      # 零第三方依赖主体，不连 SFTP / IOP
python -m call_stat --self-check            # 调用量统计组件自检
```

`check_diagnosis_offline.py` 用 8 个样例 AVL 建临时库并补一组业务键自洽数据，断言：注册 / 解析 / 入库、单一 flat 解析器、`F09633` 分表（128 张表、号码不串表、按 telnum 精确定位、缺 `product_no` 降级 `[]`、两侧分表口径一致）、卖点维表 → 商机标签 ID（按 `db_config` 声明建临时 `dimensions.db`、换行 / 竖线拆分、库缺失降级）、诊断兜底（话术原文解析 /「兜底话术」优先 / db 查空、交集空、话术原文全空三种触发、四段输出且无痛点 / 营销、维表无该 ID 仍 `code: "2"`）、多行查询、7 表关联、六段话术、业务库未就绪时商机 / 控损按空记录出话术、只有商机回捞触发 IOP 查询、在线侧 `code=0` 四段兜底，结论 `ALL PASS`。

脚本里写入侧配置（`load_profiles(runtime_dir=TMP)`）与在线侧配置（`db_config.load(avl_db_root=TMP/sqlite_db, ...)`）各自独立装配，共同指向同一批临时 db 文件；临时 `sftp_avl2db/.env` 只服务写入侧，结束后删除；维表库用临时路径注入，不写包内 `runtime/`。

## 10. 新增一类业务

1. 写入侧注册 AVL / 表结构（见 `sftp_avl2db/README.md`）；同时在 `services/db_config.py` 补在线侧声明：`TABLE_SCHEMAS` 加列顺序、`AVL_DB_SPECS` 加 `db_file` / 查询键 / 排序 / 分表（分表数还要写进 `.env` 与 `.env.example`）。两侧各自维护，但列顺序与分表口径必须一致。
2. 在 `services/` 下按业务名加一个包，`rewrite.py` 里继承 `ProductInfoBase` 实现话术重组，构造只收本业务真正消费的参数；需要辅助画像才组合 `AuxiliaryPortrait`（只有商机回捞在 `need_labels()` 里声明查 IOP）。`__init__.py` 显式导出语义化类名。
3. `routing.py`：加静态映射（若需要）、在 `build_product_object()` 里按序插入分支、把新对象类加进 `BUSINESS_OBJECT_TYPES`。
4. `services/__init__.py` 加一条延迟导出。
5. 若新业务需要标签子集，在卖点维表 `selling_points` 里给对应 `product_id` 配好「标签ID」列；若也要诊断兜底，再配「产品名称」「兜底话术」与「标签N」原文。
6. 更新根 README 与本文件的路由表、`tests/check_diagnosis_offline.py` 的断言。
