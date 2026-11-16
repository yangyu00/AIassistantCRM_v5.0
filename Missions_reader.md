# 在线业务读取与重组说明（FastAPI）

> 写入侧（SFTP 拉取 3 个进程、维表写入 1 个进程）见 `Missions_writer.md`。
> 在线 API（`python main.py`）不写任何库、不含 startup 定时任务，
> 所有业务数据与维表一律只读 SQLite。

## 架构与数据流向

```
tasks/sqlite_db/{任务名}/*.db ──→ services/data_query.py ───────┐
                                    ↑ 按 TASKS[...] + QUERY_SPECS 派发  ├─→ main.py（路由）→ services/（重组）→ output_script
dimensions.db ──→ dim_reader.py ──→ dim_transform ───────────────┘
```

| 进程 | 数量 | 职责 |
|---|---|---|
| 写入侧进程 | 4 | 3 个 SFTP 拉取 + 1 个 `dim_watcher`，见 `Missions_writer.md` |
| 在线 API | 1 | 只读两类 SQLite 库，按路由选业务对象并拼装话术 |

**部署时 5 个进程都要起**。库未就绪时 API 按「无业务记录 / 无维表」降级，不会 500。
维表更新后无需重启在线进程，下一次请求即读到新值。

## 请求处理与路由（`main.py::marketAssistant`）

1. `IOP.get_custinfo` 取用户标签 `custom_label_info`，异常时置 `{}`。
2. 按 `productId` 列表逐个建对象，命中即 `continue`（顺序不可调换）：

| 顺序 | 条件（ID 来自配置） | 数据源 | 业务对象 |
|---|---|---|---|
| 1 | `F09554_PRODUCT_IDS`（202608041615335173 / 172 / 174） | `data_query.get_f09554_latest(telnum, product_id)` | `ProductInfoBySpec9554` |
| 2 | `F09591_PRODUCT_IDS`（202609180955095402） | `data_query.get_f09591_latest(telnum)`，有记录才命中 | `ProductInfoBySpec9591` |
| 3 | 其余 ID | `data_query.get_f09609_record(telnum, product_id)`，有记录才命中 | `ProductInfoBySpec9609` |
| 4 | `load_product_selling_point()` 命中 business_id | 维表库 | `ProductInfoByExcel` |
| 5 | 兜底 | 维表库 | `ProductInfoRaw` |

前两组 ID 不再硬编码在 `main.py`，分别由
`QUERY_SPECS["f09554_opp"]["table_map"]` 的 key 与
`QUERY_SPECS["f09591_loss"]["product_ids"]` 派生。

3. 输出：`product_name` 非空 → `code: "0"` 带话术；为空 → `code: "2"`、产品名与话术为空。

## 业务库读取（`services/data_query.py`）

三个任务原先各有一个 `data_query.py`，逻辑重复、改字段要同步三处。现在合并为
**一个模块 + 一份配置**：查询的库目录、文件前缀、分表数、表列名、SQL 条件全部由
`tasks/config.py` 派生，`services/data_query.py` 只负责按任务名派发和执行。

### 配置来源（与写入侧共用）

| 配置 | 位置 | 在线读取侧用途 |
|---|---|---|
| `.env` 的 `TASK{1,2,3}_FILE_PREFIXES` / `NUM_TABLES` / `TABLE_NAME_PREFIX` | `TASKS[任务名]` | 库文件名、分表数、表名前缀；改 `.env` 读写两侧同时生效 |
| `DB_SCHEMAS[任务名]` | `tasks/config.py` | `(列名, 类型)` 列表，既是写入侧建表定义，也是 `SELECT *` 结果的 `dict` 组装顺序 |
| `QUERY_SPECS[任务名]` | `tasks/config.py` | 查询模式、条件列、排序、业务 ID 映射 |
| `db_dir(任务名)` | `tasks/config.py` | 固定指向 `tasks/sqlite_db/{任务名}/`，与写入路径一致 |

`TASKS` / `DB_SCHEMAS` / `QUERY_SPECS` 三者 key 完全一致（`f09554_opp` /
`f09591_loss` / `f09609_diag`），导入时校验列与前缀的合法性，漂移即抛 `ValueError`。

### `QUERY_SPECS` 字段

| 字段 | 含义 |
|---|---|
| `mode` | `by_product`：按 `product_id` 选前缀；`sharded`：按 `crc32(product_no) % NUM_TABLES` 分表 |
| `where` | 条件列名数组，值由模块按列名从 `(telnum, product_id)` 取，用参数绑定传入 SQL |
| `order_by` | 排序表达式，`None` 表示不排序；两者都 `LIMIT 1` |
| `table_map` | `by_product` 专用：`product_id → 文件前缀`，表名 = `data_{前缀}`，库文件 = `{前缀}.db` |
| `product_ids` | 供 `main.py` 路由判断的业务 ID 列表 |

### 三个任务的解析结果

| 任务名（= `TASKS` key） | mode | 库文件 | 表名 | 条件与排序 | 返回列 |
|---|---|---|---|---|---|
| `f09554_opp` | `by_product` | `tasks/sqlite_db/f09554_opp/{前缀}.db` | `data_{前缀}`，前缀来自 `table_map` | `product_no = telnum` + `ORDER BY op_time DESC` | 12 列（op_time…city_id） |
| `f09591_loss` | `sharded` | `tasks/sqlite_db/f09591_loss/F09591.db` | `data_F09591_{crc32 % NUM_TABLES}` | `product_no = telnum` + `ORDER BY op_time DESC` | 5 列（op_time, user_id, product_no, city_id, strategy_name） |
| `f09609_diag` | `sharded` | `tasks/sqlite_db/f09609_diag/F09609.db` | `data_F09609_{crc32 % NUM_TABLES}` | `product_no = telnum AND product_id = ?`，不排序 | 5 列（op_time, user_id, product_no, product_id, strategy_script） |

### 对外接口

| 接口 | 说明 |
|---|---|
| `query(task_name, telnum, product_id=None)` | 通用派发；`task_name` 必须是 `QUERY_SPECS` 的 key |
| `get_f09554_latest(telnum, product_id)` | 上表三行的薄封装，保持 `main.py` 调用点语义清晰 |
| `get_f09591_latest(telnum)` | 同上 |
| `get_f09609_record(telnum, product_id)` | 同上 |
| `F09554_PRODUCT_IDS` / `F09591_PRODUCT_IDS` | 由 `table_map` / `product_ids` 派生的不可变集合，供路由判断 |

均为 `async` 函数、内部同步 `sqlite3`；表名与列名来自代码内配置，用户输入只走参数绑定。

### 加任务或改字段

- 改列名/列顺序 → 只改 `DB_SCHEMAS[任务名]`（写入侧建表与读取侧 `dict` 组装同时生效）。
- 改条件或排序 → 只改 `QUERY_SPECS[任务名]`。
- 加分表数/换表名前缀/换文件前缀 → 改 `.env` 对应 `TASK*_` 键，读写两侧同时生效。
- 新增一个任务 → 在 `TASKS` / `DB_SCHEMAS` / `QUERY_SPECS` 各加一项，`main.py` 用
  `query(任务名, ...)` 调用即可，无需新增查询模块。

主键、分表数与库表定义的完整口径见 `Missions_writer.md`。

## 业务库话术重组（3 个 spec 子类）

| 子类 | 读取字段 | 重组规则 |
|---|---|---|
| `rewrite_by_spec_f09554` | `content_abstract` / `type_xl` / `cust_demand` / `rec_strategy` / `reject_reason` / `reason_note` | 拼【通话总结】【用户需求】【已推荐政策】【拒绝原因】四段，标题加粗、部分蓝色，缺值写「无」；末尾拼 `auxiliary_portrait`，换行统一替换为 `<br>` |
| `rewrite_by_spec_f09591` | `strategy_name` | 自定义标签 `<$B>` / `<$I>` / `<$U>` / `<#RRGGBB>` / `<$B$I#RRGGBB>` → `<b>` / `<i>` / `<u>` / `<span style="color: #RRGGBB">`，`</>` 按栈逆序闭合；空内容返回「无历史话术记录」 |
| `rewrite_by_spec_f09609` | `strategy_script` | 按 `\n` 拆行、按 `[：:]` 切一次，「标题：」蓝色加粗；标题为「资费摘要」时标题与内容均红色加粗；换行替换为 `<br>`；空内容返回「无话术记录」 |

这些格式化都在业务侧执行，库里保存的是原始文本。

## 读取侧（`dim_reader`）

三个加载函数，全部 `mode=ro` 打开、用完即关，**在线业务绝不创建/写入维表库**。

| 函数 | 返回形状 | 消费方 |
|---|---|---|
| `load_lookup("label_name_mapping")` | `{label_id: display_name}` | `ProductInfoRaw` 辅助画像 |
| `load_product_selling_point()` | `{business_id: (product_name, selling_point, label_id)}` | `main.py` 路由、`ProductInfoRaw`、`ProductInfoByExcel` |
| `load_cut_in_script()` | `{business_id: {标签ID / 兜底话术: [切入, 挽留] 或 ""}}` | `ProductInfoByExcel` |

`load_cut_in_script()` 用 `ORDER BY rowid` 取
`{key, fallback, groups} + tag_*` 原始列，把每行转成列名映射后交给
`dim_transform.build_cut_in_script_dict()`；列名、标签列顺序、兜底键全部取自
`registry.json`，不硬编码。

### 降级矩阵

| 场景 | 行为 |
|---|---|
| 库文件不存在 | 返回 `{}` |
| 表不存在 / 列缺失（如缺 `tag_*`） | 返回 `{}`，提示删库重建 |
| 未注册表 / 未声明 `lookup` / `cut_in_script` | 返回 `{}` |
| 查询异常、`registry.json` 缺失或损坏 | 返回 `{}` |
| 注册 xlsx 缺失 | 写入侧告警跳过，库保留上次成功版本 |

返回 `{}` 时业务行为：产品名为 `None`、`queried_label_ids` 为 `[]`、
卖点/切入/挽留话术为空、辅助画像的标签名沿用上游 `custom_label_info` 给的名字。
**维表侧出任何问题都不会让 API 500**。同一类告警每进程只打印一次（`_WARNED` 集合），避免刷屏。

### 读取次数

不做进程级缓存，每次调用读一次全表，天然读到 `dim_watcher` 写入的最新值，
无缓存失效或脏读问题。一次请求内：

- `main.py` 的 `marketAssistant` 循环**外**加载 1 次，供「维表中业务」路由判断复用；
- 每个 `ProductInfoRaw` 实例加载 1 次卖点表 + 1 次标签映射表（循环外各一次）；
- `ProductInfoByExcel` 复用父类那份卖点表，另加载 1 次 `cut_in_script`（36 列原始列）。

标签映射表 60 行、卖点表 1361 行，本地 SQLite 开销可忽略。
后续若要省连接，可基于**各表独立的 version** 加缓存。

## 维表读取侧依赖的 registry 声明

完整配置项与校验见 `Missions_writer.md`；在线读取只消费其中两类声明：

| 声明 | 形状 | 用途 |
|---|---|---|
| `lookup.value` | 单列 → `{key: 单值}` | `label_name_mapping` |
| `lookup.values` | 多列 → `{key: (值1, 值2, 值3)}` | `product_selling_point_match`，`values` 顺序即元组下标 |
| `cut_in_script` | `key` / `fallback` / `groups` / `tags` / `fallback_key` | 声明加工所需的原始列角色，`tags` 可写 `"dynamic_columns"` 直接引用展开结果 |

⚠️ `cut_in_script.fallback_key` 可配，但 `services/rewrite_by_excel/rewrite.py` 用
`"兜底话术"` 做比较，改它需同步改业务代码。

## 维表数据重组（`dim_transform`）

只依赖 `re`，不写回库。库里存的是含换行、`**`、原始 HTML 片段的单元格文本；
`strip()`、`.replace("**", "")`、HTML 包装都在这一步做。

### `cut_in_str2tuple(text)`

把一段话术文本拆成 `[切入话术, 挽留话术]`：

1. 正则 `切入话术(.*?)拒绝挽留话术(.*)`（`re.DOTALL`）拆两段。
2. 每段逐行处理：跳过空行；`:` 统一为 `：` 后按 `：` 切分；
   只有 1 段时原样保留；有 `：` 时用 `^\d+\s?\.\s?` 去掉行首编号，
   把「前缀：」包成 `<strong><span style="color: blue;">前缀：</span></strong>` 再拼回正文。
3. 行间用 `\n` 连接；切入段加 `【切入话术】\n` 前缀，挽留段加 `【挽留话术】\n` 前缀。
4. 匹配不到标记，或两段都为空 → 返回空字符串 `""`（**不是 list**）。

业务侧 `fusion_script_list[0]` 取切入、`[1]` 取挽留；值为 `""` 时 `""[0]`
会 `IndexError` 并被业务侧 `except` 吞掉返回空串。

### `build_cut_in_script_dict(rows, ...)`

按原始行重建嵌套 dict：

1. 以 `key_col`（`business_id`）为外层键；先算兜底项
   `{fallback_key: cut_in_str2tuple(fallback.strip().replace("**", ""))}`。
2. `groups`（`match_result`）非空 → 按 `\n` 拆成 ID 列表；对每个下标 g：
   `tag_cols[g]` 有内容则 `cut_in_str2tuple(...)` 存入 `inner[one_id]`，否则存 `""`。
   **分组数超过 `tag_cols` 长度时，多出来的分组填 `""`**。
3. `groups` 为空 → **整条置为 `{}`**，兜底项一并丢弃。
4. 重复 `business_id` 按行序后写覆盖。

样例维表有 28 个标签列，个别行拆出 29~30 个分组，规则 2 会生效；
`groups` 为空的行在库里有 403 条，规则 3 会生效。

## 在线业务接入点（维表）

| 位置 | 加载函数 | 消费方式 |
|---|---|---|
| `main.py::marketAssistant` | `load_product_selling_point()` | `if one_product_obj.id in dict` 判断是否走「维表中业务」分支 |
| `services/product_info_raw.py::__init__` | 同上，存 `self.product_selling_point_match_dict` | `[0]` 取产品名；`[2].split("\n")` 取 `queried_label_ids`（不提前拆、不过滤空串、不额外 strip 每项） |
| `services/product_info_raw.py::__get_auxiliary_portrait` | `load_lookup("label_name_mapping")` | 命中则替换辅助画像的标签名，后续所有标签特殊规则不变 |
| `services/rewrite_by_excel/rewrite.py::__init__` | `load_cut_in_script()`，存 `self.cut_in_script_dict` | 切入取 `[0]`、挽留取 `[1]` |
| `services/rewrite_by_excel/rewrite.py::__get_selling_point` | 复用父类 `self.product_selling_point_match_dict` 的 `[1]` | 正则 `\d、.*?：` 找编号段，首段包 `<strong>` + `<span style="color:red;">`，后续段落逐段 `<strong>` 拼接补换行；匹配不到编号只加 `【卖点】\n`。输出再经 `__strong_text()` 把四个小节标题包 `<strong>` |

**格式转换全部留在业务侧**，库里 `selling_point` 存原始「生成话术」文本，
以后改配色/标签不用重导全表。

## 公共画像拼装（`services/product_info_raw.py`）

所有业务对象都继承 `ProductInfoRaw`，构造时固定走这段重组：

- `queried_label_ids`：卖点维表元组的 `[2]`，业务侧 `.split("\n")` 得到
  （读取侧不提前拆、不过滤空串、不额外 strip 每项，保证与画像逐字一致）。
- `queried_label_nvs`：按 `queried_label_ids` 顺序与 `custom_label_info` 对齐，缺失填 `None`。
- `auxiliary_portrait`：逐个标签拼 `标签名: <strong><span style="color:blue;">值</span></strong>; `，
  命中 `label_name_mapping` 时替换标签名；随后应用一组按标签 ID 硬编码的特殊规则
  （掉线记录 0.00 → 「无」、费用类补「元」、抖音快手流量补「M」、1/0 → 是/否、
  重复标签去重、降档阈值标题标红），最后补 `【辅助画像】` 前缀。

`ProductInfoByExcel` 在此基础上追加卖点、切入、挽留三段，经 `__strong_text()`
把四个小节标题包成 `<strong>` 后作为 `output_script`。

## 降级与容错汇总

| 侧 | 触发 | 行为 |
|---|---|---|
| 维表 | 库/表/列不存在、`registry.json` 异常、查询失败 | `dim_reader` 返回 `{}`，业务按「无维表」降级 |
| 业务库 | 库文件不存在、`product_id` 未映射、未注册任务名、查询异常 | `services/data_query.py` 返回 `{}`，对应 spec 子类走空记录分支 |
| 写入侧 | 注册 xlsx 缺失 | `dim_watcher` 告警跳过，库保留上次成功版本 |

**维表侧出任何问题都不会让 API 500**：产品名为 `None`、`queried_label_ids` 为 `[]`、
卖点/切入/挽留话术为空、辅助画像标签名沿用上游给的名字。
同一类告警每进程只打印一次（`_WARNED` 集合），避免刷屏。

不做进程级缓存，每次调用读一次全表，天然读到写入侧的最新值，
无缓存失效或脏读问题；改库后下一次请求即生效，无需重启。

## 自检要点（在线侧）

- 删库后调用 `load_product_selling_point()` / `load_cut_in_script()` 应返回 `{}`
  且不创建任何文件。
- `import main` 正常，`app.router.on_startup` 为空（无定时任务）。
- 只改卖点表中的 `product_name` 后直接请求接口，下一次调用即读到新名字。
- `services/data_query.py`、三个 spec 子类、`dim_reader` 均可独立导入。
- `TASKS` / `DB_SCHEMAS` / `QUERY_SPECS` 三者 key 一致；改 `.env` 的
  `TASK2_NUM_TABLES` 后，`get_f09591_latest()` 解析出的表名后缀同步变化。

## 未实现

- 基于 `version` 的进程级缓存、版本回滚、历史版本查询、灰度切换。
- 加工规则的完全配置化：当前 `cut_in_str2tuple` 与分组规则写在 `dim_transform.py`，
  `registry.json` 只声明列角色与列顺序。
