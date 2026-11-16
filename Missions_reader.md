# 在线业务读取与重组说明（FastAPI）

> 写入侧（SFTP 拉取 3 个进程、维表写入 1 个进程）见 `Missions_writer.md`。
> 在线 API（`python main.py`）无任何定时任务，只有一个 `lifespan` 生命周期钩子
> （启动时恢复调用量基数、关闭时落库，见「调用量统计」）；
> 业务库与维表一律只读 SQLite，唯一写入是调用量统计（本地 `log/call_sum.db`）。
> 三个任务的业务命名与配置键前缀见 `Missions_writer.md` 的「任务命名」。

## 架构与数据流向

```
tasks/sqlite_db/{任务名}/*.db ──→ services/data_query.py ───────┐
                                    ↑ 按 TASKS[...] + QUERY_SPECS 派发  ├─→ main.py（路由）→ services/（重组）→ output_script
dimensions.db ──→ dim_reader.py ──→ dim_transform ───────────────┘
```

| 进程 | 数量 | 职责 |
|---|---|---|
| 写入侧进程 | 4 | 3 个 SFTP 拉取 + 1 个 `dim_watcher`，见 `Missions_writer.md` |
| 在线 API | 1 | 只读业务库与维表、按路由选业务对象并拼装话术；另在内存累计调用量并按阈值写入 `log/call_sum.db` |

**部署时 5 个进程都要起**。库未就绪时 API 按「无业务记录 / 无维表」降级，不会 500。
维表更新后无需重启在线进程，下一次请求即读到新值。

## 请求处理与路由（`main.py::marketAssistant`）

1. `IOP.get_custinfo` 取用户标签 `custom_label_info`；函数内所有失败分支统一返回
   `{}`，调用点另有 `try/except` 兜底。详见「用户标签获取（IOP）」。
2. 按 `productId` 列表逐个建对象，命中即 `continue`（顺序不可调换）：

| 顺序 | 条件（ID 来自配置） | 数据源 | 业务对象 |
|---|---|---|---|
| 1 | `OPPORTUNITY_RECALL_PRODUCT_IDS`（202608041615335173 / 172 / 174） | `data_query.get_opportunity_recall_latest(telnum, product_id)` | `ProductInfoBySpec9554` |
| 2 | `LOSS_CONTROL_PRODUCT_IDS`（202609180955095402） | `data_query.get_loss_control_latest(telnum)`，有记录才命中 | `ProductInfoBySpec9591` |
| 3 | 其余 ID | `data_query.get_customer_diagnosis_record(telnum, product_id)`，有记录才命中 | `ProductInfoBySpec9609` |
| 4 | `load_product_selling_point()` 命中 business_id | 维表库 | `ProductInfoByExcel` |
| 5 | 兜底 | 维表库 | `ProductInfoRaw` |

前两组 ID 不再硬编码在 `main.py`，分别由
`QUERY_SPECS["opportunity_recall"]["db_map"]` 的 key 与
`QUERY_SPECS["loss_control"]["product_ids"]` 派生。

3. 输出：`product_name` 非空 → `code: "0"` 带话术；为空 → `code: "2"`、产品名与话术为空。

## 用户标签获取（IOP）

`IOP/get_custinfo.py::get_custom_info_in_global(tel)` 一次请求只调用一次，结果在
`productId` 循环内复用。返回 `{ITEMCODE: [ITEMNAME, ITEMVALUE], ...}`。

### 返回契约与降级

**所有失败分支统一返回空 dict `{}`**，不再返回 `None`。原因是 `main.py` 的
`try/except` 只包住函数调用本身，`None` 不抛异常、不会触发 `except`，会一路传到
`ProductInfoRaw::__get_queried_label_nvs` 并抛 `TypeError`（只要该业务在维表里有
标签项），使"token 拿不到"这个最该降级的场景反而返回 500。空 dict 则让下游
按「无标签」正常产出话术。

失败分支包括：token 不可用、token 请求超时/网络异常、标签查询连接或读超时、
响应 JSON 解析失败、非 200、`res_code != 0`、响应非 dict、缺少或非列表的
`BUSIINFO`。缺 `ITEMCODE` / 非 dict 的单条记录跳过，不拖累整批。
`res_code` 按 `str(...)` 比较，兼容上游返回字符串 `"0"` 或数字 `0`。

### 日志字段

每条查询必留一条日志（成功 `INFO`、失败 `ERROR`），字段统一为 `key=value`：

| 字段 | 说明 |
|---|---|
| `status` | HTTP 状态码 |
| `res_code` / `res_desc` | 仅失败分支记录，取上游响应 |
| `busi_info` / `labels` | 上游条数 / 成功入库的标签条数 |
| `cost` | 单次查询耗时（毫秒） |
| `error` | 响应解析失败、连接/读超时、token 请求失败时记录 `异常类型: 信息` |

日志**不含手机号、不含 access_token**。响应解析仍用 `json(content_type=None)`——
上游返回的 Content-Type 不规范，不做类型校验。新增两类网络失败日志：
`IOP token 请求失败` 与 `IOP 标签查询网络失败`，都带 `error` 与 `cost`，
保证超时时 IOP 侧仍留痕（否则异常会穿透到 `main.py` 的 `except`，只剩一条笼统日志）。

### 配置读取

`global_config = ImmutableYamlConfig()` 只在 `IOP/http_session.py` 里读一次，
`get_token.py` / `get_custinfo.py` 直接复用同一个只读对象。`ImmutableYamlConfig`
是单例且返回只读配置，但每次调用其构造函数都会重新 `open` + `yaml.load` 并
重建字典，原先每请求至少一次磁盘 I/O + yaml 解析（token 刷新时再多一次）。
现在请求路径为 0 次，IOP 侧 import 阶段的读盘也由两次收敛为一次。
运行期修改 `config.yaml` 不会热生效，需重启进程。`client_timeout()` 复用同一份
模块级只读配置，构造 `aiohttp.ClientTimeout`，无磁盘 I/O。

### 异步与超时（A1 / C1）

原先 `IOP/get_token.py::token2global()` 虽是 `async def`，内部却用同步
`requests.post`：一旦触发 token 刷新，**整个事件循环被卡住**，所有并发请求一起等。
现改为 `aiohttp` 异步请求，两个 HTTP 端点（token / 标签查询）都配 `ClientTimeout`：

| 位置 | 改动 |
|---|---|
| `get_token.py` | `requests.post` → `aiohttp` 异步请求；服务链路不再混用 `requests` / `aiohttp` |
| `IOP/http_session.py` | 统一构造 `ClientSession(timeout=client_timeout(), skip_auto_headers=...)`，connect / sock_read / total 三层超时；两处调用点只取 `session_context()`（见 C2 小节） |
| 两处调用点 | 超时与网络异常（`aiohttp.ClientError` / `asyncio.TimeoutError`）就地捕获、记日志、降级；token 侧还清空 `access_token`，让既有重试继续 |

配置在 `config.yaml` 的 `IOP.timeout`（秒），两个端点共用一组：

| 键 | 默认 | 说明 |
|---|---|---|
| `total` | `3` | 单次请求总超时 |
| `connect` | `1` | 建连超时 |
| `sock_read` | `2` | 读响应超时 |

`client_timeout()` 对缺键用同名默认值兜底，部署沿用旧 `config.yaml` 也不会启动失败。
`total` 调大放宽慢上游、调小更快降级；**注意与重试的乘积**：`get_custinfo.py` 的
token 刷新仍是「循环外 1 次 + 循环内最多 3 次、无退避」（D1 未改），
因此 token 端点持续无响应时，单次业务请求会串行等 4 次超时。每次由
`sock_read` 与 `total` 中**先到者**触发：默认 `sock_read=2` 先到，实测约 8 秒，
上界 `4 × total = 12` 秒。要压低这个窗口，调小 `sock_read` / `total`，
或后续单独处理 D1 的重试次数与退避。

**本轮同时实现 C2**：共享 `ClientSession` 见下一小节。仍未越界的是
A2 单飞锁（`TOKENS` 仍无并发保护）、D1 重试次数/退避、D3 token 响应状态码校验、
F2 `IOPClient` 类。`test_local/test_iop.py` 仍用 `requests`，它是独立调试脚本、
不在服务链路，保留原样。

### 共享 ClientSession（C2）

原先每次请求都 `async with aiohttp.ClientSession(...)`，无连接复用、每次重新
TCP 握手，高并发下句柄开销明显。现在应用级只创建一个会话：

| 阶段 | 动作 |
|---|---|
| lifespan startup | `await init_session()`：在当前 event loop 创建，带 `client_timeout()` 与 `skip_auto_headers` |
| 运行期 | `get_token.py` / `get_custinfo.py` 统一 `async with session_context() as client`；共享 session 直接 yield，**不在此关闭** |
| lifespan shutdown | `await close_session()`：校验与创建时同一 loop，`await session.close()` |

**同 event loop**：`ClientSession` 绑定创建它的 loop，必须在同一 loop 内关闭。
`atexit` 拿不到运行中的 loop，不能用于收尾，因此只挂 `main.py` 的 lifespan；
uvicorn 正常关闭时 startup / shutdown 在同一 loop。`init_session()` /
`close_session()` 都幂等，创建或关闭失败只记日志、不外抛，不阻断启动或停止。

**降级路径**：未经 lifespan 时（TestClient 不进上下文、独立脚本 import 后直接
调用），`get_session()` 为 `None`，`session_context()` 退回「本次请求一个临时
session」，由内层 `async with` 在当前 loop 内创建并关闭，只告警一次避免刷屏。
行为与改动前一致、不泄漏连接，但**失去连接复用**——生产必须确保走 lifespan，
自检可用这条 WARNING 判断部署是否漏了入口。

**一个 session 服务两个端点**：token 用 `data=`、标签查询用 `json=` + 显式
headers，共用同一份 `skip_auto_headers=("Content-Type",)`。该参数只跳过
**自动生成**的头：token 请求不带 Content-Type（等价 `requests`），标签查询
显式传入的 `charset=gbk` / `Accept-Encoding` 不受影响、原样发出（E4）。
实测 5 次请求共享 session 的连接池只有 1 条活跃连接，改动前每次新建 session
对应 5 条 TCP 连接。`config.yaml` 不新增配置项，复用 `IOP.timeout`。

### 上游既定约定（不可改动）

代码里对应位置已加注释锁定，后续优化不要"顺手修好"：

- `access_token` 必须放在 URL 查询串，上游不接受 `Authorization` 头。
- 两个端点只提供 `http` 明文；`client_id` / `client_secret` 以表单串提交，
  凭据存放于 `config.yaml`。
- 请求头必须声明 `Content-Type: application/json; charset=gbk` 并显式携带
  `Accept-Encoding: gzip, deflate`，即使与实际序列化编码不一致也照发。

换成 `aiohttp`、再收敛为共享 `ClientSession` 后，上述各项逐字保留。额外一处
等价处理：`requests` 对字符串 `data=` 不加 `Content-Type`，而 `aiohttp` 默认会补
`text/plain; charset=utf-8`，所以共享 session 统一带
`skip_auto_headers=("Content-Type",)`。它只跳过自动生成的头，标签查询显式传入
的 `charset=gbk` / `Accept-Encoding` 仍原样发出，实测两种请求的上游请求头与
改动前逐一一致。这不是新约定，是把库切换与会话共用带来的副作用消掉。

## 业务库读取（`services/data_query.py`）

三个任务原先各有一个 `data_query.py`，逻辑重复、改字段要同步三处。现在合并为
**一个模块 + 一份配置**：查询的库目录、文件前缀、分表数、表列名、SQL 条件全部由
`tasks/config.py` 派生，`services/data_query.py` 只负责按任务名派发和执行。

### 配置来源（与写入侧共用）

| 配置 | 位置 | 在线读取侧用途 |
|---|---|---|
| `.env` 各任务分组的 `*_FILE_PREFIXES` / `*_NUM_TABLES` / `*_TABLE_NAME_PREFIX`（业务前缀见 `Missions_writer.md`） | `TASKS[任务名]` | 库文件名、分表数、表名前缀；改 `.env` 读写两侧同时生效 |
| `DB_SCHEMAS[任务名]` | `tasks/config.py` | `(列名, 类型)` 列表，既是写入侧建表定义，也是 `SELECT *` 结果的 `dict` 组装顺序 |
| `QUERY_SPECS[任务名]` | `tasks/config.py` | 查询模式、条件列、排序、业务 ID 映射 |
| `db_dir(任务名)` | `tasks/config.py` | 固定指向 `tasks/sqlite_db/{任务名}/`，与写入路径一致 |

`TASKS` / `DB_SCHEMAS` / `QUERY_SPECS` 三者 key 完全一致（`opportunity_recall` /
`loss_control` / `customer_diagnosis`），导入时校验列与前缀的合法性，漂移即抛 `ValueError`。

### `mode` 的三种库表形式

`mode` 按「库数量 × 表数量」划分，枚举写在 `tasks/config.py` 的 `QUERY_MODES`。
**多库多表场景不存在，不在支持范围内**，配置里出现任何其它值会在导入时抛 `ValueError`。

| mode | 库表形式 | 库文件 | 表名 | 专用字段 |
|---|---|---|---|---|
| `multi_db_single_table` | 多库单表 | 按 `product_id` 选 `{库名}.db` | 每库一张 `data_{库名}` | `db_map` |
| `single_db_multi_table` | 单库多表 | `{FILE_PREFIXES[0]}.db` | `{TABLE_NAME_PREFIX}_{crc32 % NUM_TABLES}` | 无（用 `.env` 的分表配置） |
| `single_db_single_table` | 单库单表 | `{FILE_PREFIXES[0]}.db` | `table_name` 指定的固定表 | `table_name` |

当前三个任务只用到前两种；`single_db_single_table` 已实现但暂无任务使用，
新增单库单表任务时补 `table_name` 即可。`services/data_query.py::_resolve()` 按
`mode` 走三个分支，各自算出 `(db_path, table_name, where 值列表)`。

> **库名与表名分开**：`db_map` 是**库名映射**（`product_id → 库名`），值是 `.db`
> 文件名主体，不是表名。多库单表模式下表名按固定约定 `data_{库名}` 派生，
> 与写入侧 `init_db()` 建的 `data_{前缀}` 一致。

### `QUERY_SPECS` 字段

| 字段 | 含义 |
|---|---|
| `mode` | 库表形式，必须是 `QUERY_MODES` 三者之一，见上表 |
| `where` | 条件列名数组，值由模块按列名从 `(telnum, product_id)` 取，用参数绑定传入 SQL |
| `order_by` | 排序表达式，`None` 表示不排序；两者都 `LIMIT 1` |
| `db_map` | 多库单表专用：**库名映射** `product_id → 库名`；库文件 `{库名}.db`，表名 `data_{库名}`。库名必须存在于 `FILE_PREFIXES` |
| `table_name` | 单库单表专用：固定表名 |
| `product_ids` | 供 `main.py` 路由判断的业务 ID 列表 |

### 三个任务的解析结果

| 任务名（= `TASKS` key） | mode | 库文件 | 表名 | 条件与排序 | 返回列 |
|---|---|---|---|---|---|
| `opportunity_recall` | `multi_db_single_table` | `tasks/sqlite_db/opportunity_recall/{库名}.db` | `data_{库名}`，库名来自 `db_map` | `product_no = telnum` + `ORDER BY op_time DESC` | 12 列（op_time…city_id） |
| `loss_control` | `single_db_multi_table` | `tasks/sqlite_db/loss_control/F09591.db` | `data_F09591_{crc32 % NUM_TABLES}` | `product_no = telnum` + `ORDER BY op_time DESC` | 5 列（op_time, user_id, product_no, city_id, strategy_name） |
| `customer_diagnosis` | `single_db_multi_table` | `tasks/sqlite_db/customer_diagnosis/F09609.db` | `data_F09609_{crc32 % NUM_TABLES}` | `product_no = telnum AND product_id = ?`，不排序 | 5 列（op_time, user_id, product_no, product_id, strategy_script） |

### 对外接口

| 接口 | 说明 |
|---|---|
| `query(task_name, telnum, product_id=None)` | 通用派发；`task_name` 必须是 `QUERY_SPECS` 的 key |
| `get_opportunity_recall_latest(telnum, product_id)` | 上表三行的薄封装，保持 `main.py` 调用点语义清晰 |
| `get_loss_control_latest(telnum)` | 同上 |
| `get_customer_diagnosis_record(telnum, product_id)` | 同上 |
| `OPPORTUNITY_RECALL_PRODUCT_IDS` / `LOSS_CONTROL_PRODUCT_IDS` | 由 `db_map` / `product_ids` 派生的不可变集合，供路由判断 |

均为 `async` 函数、内部同步 `sqlite3`；表名与列名来自代码内配置，用户输入只走参数绑定。

### 加任务或改字段

- 改列名/列顺序 → 只改 `DB_SCHEMAS[任务名]`（写入侧建表与读取侧 `dict` 组装同时生效）。
- 改条件或排序 → 只改 `QUERY_SPECS[任务名]`。
- 加分表数/换表名前缀/换文件前缀 → 改 `.env` 对应业务前缀分组的键，读写两侧同时生效。
- 换库表形式 → 改 `QUERY_SPECS[任务名]["mode"]` 并补齐对应的专用字段
  （多库单表要 `db_map`、单库单表要 `table_name`、单库多表用 `.env` 分表配置），
  三种形式的分支已经在 `services/data_query.py::_resolve()` 里实现。
- 新增一个任务 → 在 `TASKS` / `DB_SCHEMAS` / `QUERY_SPECS` 各加一项并选好 `mode`，
  `main.py` 用 `query(任务名, ...)` 调用即可，无需新增查询模块。

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

## 调用量统计（`src/call_stat.py` + GET `/api/callStat`）

### 存储

`log/call_sum.db`，表 `call_sum(date TEXT PRIMARY KEY, call_count INTEGER)`，**一天一条**。
一年不到 400 行，不做删除：`scripts/delete_before_date.py` 已删除。
导出也不再走脚本：`scripts/export_call_csv.py` 已删除，改由下面的 GET 接口直接查。
`scripts/show_top_10.py` 是查业务库的独立调试脚本，与本模块无关，保留。

### 缓冲写入与进程生命周期

`main.py` 用 FastAPI `lifespan` 管理统计模块的生命周期（`app = FastAPI(lifespan=lifespan)`，
不使用已弃用的 `@app.on_event`）：

| 阶段 | 动作 |
|---|---|
| startup（`yield` 之前） | `call_stat.startup()`：确保 `call_sum` 表存在，从库读回当天已落库值作为基数；日志打印「调用量统计已恢复：当天已累计 N 次」。只做一次恢复，不注册任何定时任务 |
| 运行期 | middleware 每次请求调 `call_stat.record_call()`，纯内存 +1 |
| shutdown（`yield` 之后） | `call_stat.flush()` 补齐不足阈值的尾部增量 |

`uvicorn` 的 SIGTERM 退出路径不经过 `atexit`，所以必须由 lifespan 的 `finally`
显式 flush；`src.call_stat` 仍注册 `atexit` 作为非 ASGI 运行方式的兜底，两处幂等。
`app.router.on_startup` / `on_shutdown` 均为空，钩子挂在 `router.lifespan_context`。

进程内两个全局字典（`RLock` 保护）：

| 变量 | 含义 |
|---|---|
| `_DAY_TOTAL[YYYY-MM-DD]` | 当天**完整累计调用量** = 库里已落库 + 本进程所有请求（无论是否已落库）。请求路径只改这个变量，**不读库** |
| `_PENDING[YYYY-MM-DD]` | 其中「尚未落库」的增量 |

流程：

1. 每次请求 `_DAY_TOTAL[today] += 1`、`_PENDING[today] += 1`；返回值直接来自
   `_DAY_TOTAL`，热路径完全不碰 SQLite（除非恰好触发 flush）。
2. 某日期 `_PENDING` 累计到 `flush_threshold` 才把这段增量一次性
   `UPDATE call_sum SET call_count = call_count + ?`（当天首次则 `INSERT`），随后清零。
   `_DAY_TOTAL` 不受落库影响——它记的是完整累计，落库与否都不变。
3. **跨天残留**：旧日期不足阈值就翻天时，后续请求会顺带重试落库，
   不会把今天的调用记到昨天，也不会丢掉昨天的尾部。
4. **写库失败**：保留 `_PENDING` 增量并打印告警，下一轮重试；`_DAY_TOTAL` 仍准确，
   异常不抛给正在处理的请求。库不可读时 `_read_day` 降级为 0，不阻断服务启动。

### 重启后继续计数

**不会重新计数，也不会从 0 开始。** 三层保证：

1. 表以 `date` 为主键，落库是 `UPDATE call_sum SET call_count = call_count + delta`
   的**累加**，不是覆盖；库里存的一直是该日期的累计总量。
2. `startup()` 从库读回当天已落库值作为 `_DAY_TOTAL` 基数；即使 lifespan 没执行
   （例如 TestClient 不进上下文、直接 import 后手工调用），首次 `record_call()`
   也会由 `_ensure_day()` 懒加载恢复，行为一致。
3. `get_call_count(今天)` 直接返回 `_DAY_TOTAL`；查历史日期（重启后不在内存）走
   「读库 + `_PENDING`」。

例：库里当天已落库 100 次 → 重启后 startup 恢复基数 100 → 第 1 次请求后
`GET /api/callStat?date=今天` 返回 101，不是 1。

唯一缺口是**强杀/崩溃**：不足阈值、还没来得及落库的那一段会丢（默认 99 次以内），
重启后从上一次成功落库的值继续，数字仍连续，只是少算这一段；**不会归零**。
正常关闭（SIGTERM / lifespan shutdown）不丢。阈值调小可缩小该窗口。

### 配置（`config.yaml`）

| 键 | 默认 | 说明 |
|---|---|---|
| `call_stat.flush_threshold` | `100` | 累计多少次调用写一次库，必须为正整数；启动时也可 `call_stat.configure(flush_threshold=N)` 覆盖 |
| `call_stat.url` | `/api/callStat` | GET 查询路径，与 `marketAssistant.url` 同一套配置风格 |

### GET 查询

路径取自 `config.yaml` 的 `call_stat.url`。两个 query 参数**必须且只能提供一个**；
校验失败返回 `{"res_code": 1, "res_desc": "...", "result": ""}`（HTTP 200，与项目其它接口一致）。

| 参数 | 格式 | 返回 `result` |
|---|---|---|
| `date=2026-11-16` | `YYYY-MM-DD` | `{"date": "2026-11-16", "callCount": 123}` |
| `month=2026-11` | `YYYY-MM` | `{"month": "2026-11", "days": [{"date": "2026-11-01", "callCount": 0}, ...]}` |

- 单日与月查询都读到完整累计，**当天尚未落库的部分也算在内**：本进程有活动的
  日期直接用 `_DAY_TOTAL`，历史日期用「库 + `_PENDING`」。
- 月查询按 `calendar.monthrange` 补齐整月每一天，无记录的日期补 `0`，前端可直接画折线。
- 该 GET 请求自身也经过 middleware 计数（保持「每次访问都计数」的原语义），
  但计数发生在 middleware 的 `finally` 阶段，所以**本次返回的 `callCount` 不含本次请求**；
  连续两次查询时，第二次会包含第一次。
- 日期/月份用正则 + `strptime` 严格校验（`2026-1-6`、`2026-13` 会被拒绝）。

```bash
curl "http://host:8989/api/callStat?date=2026-11-16"
curl "http://host:8989/api/callStat?month=2026-11"
```

## 降级与容错汇总

| 侧 | 触发 | 行为 |
|---|---|---|
| 维表 | 库/表/列不存在、`registry.json` 异常、查询失败 | `dim_reader` 返回 `{}`，业务按「无维表」降级 |
| 业务库 | 库文件不存在、`product_id` 未映射、未注册任务名、查询异常 | `services/data_query.py` 返回 `{}`，对应 spec 子类走空记录分支 |
| 调用量库 | `log/call_sum.db` 写入失败 | 保留 `_PENDING` 增量并告警、下轮重试；`_DAY_TOTAL` 仍准确，不影响请求 |
| 调用量库 | 启动时读库失败 | 只告警、基数按 0 恢复，服务照常启动；库恢复后下次重启从真实值继续 |
| IOP | token / 标签查询超时、连接失败 | 就地记 ERROR、token 侧清空 `access_token`、最终降级 `{}`；不阻塞其它并发请求 |
| IOP | 未经 lifespan（共享 session 未初始化） | `session_context()` 降级为本次请求临时 session，只告警一次；结果正确、不泄漏连接，但失去连接复用 |
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
  `LOSS_CONTROL_NUM_TABLES` 后，`get_loss_control_latest()` 解析出的表名后缀同步变化。
- `QUERY_MODES` 恰为三种；`_resolve()` 对三个任务分别解析出
  `{库名}.db` + `data_{库名}`、`F09591.db` + `data_F09591_N`、`F09609.db` + `data_F09609_N`。
  把 `mode` 改成多库多表等非法值，import `tasks.config` 应抛 `ValueError`。
- `app.router.on_startup` / `on_shutdown` 均为 0，`router.lifespan_context` 已设置
  （确认走 lifespan 而非 `on_event`）；startup 日志有「调用量统计已恢复」。
- 把 `call_stat.flush_threshold` 设为 3：连续 3 次请求后才写一次 `call_sum.db`；
  第 1、2 次时 `GET /api/callStat?date=今天` 的 `callCount` 已含内存增量；
  `call_stat.flush()` 后 `_PENDING` 为空；GET 不传/传两个参数、`date=2026-1-6`、
  `month=2026-13` 均返回 `res_code: 1`。
- **重启继续计数**：库里当天先落库到 N，停掉进程再用新进程启动同一目录，
  首次 `GET ...?date=今天` 应返回 N（不是 0），再发一次请求返回 N+1，
  shutdown 后库里为 N+1。
- 起 uvicorn 打几个不足阈值的请求后 `kill -TERM`：日志出现
  `Application shutdown complete.`，且 `call_sum.db` 里含这几条尾部增量
  （验证 lifespan shutdown，而非只依赖 `atexit`）。
- `client_timeout()` 定义于 `IOP/http_session.py`，读出的 `total` / `connect` /
  `sock_read` 与 `config.yaml` 的 `IOP.timeout` 一致；删掉该段后回落到 3 / 1 / 2，
  不抛异常。共享 session 与降级临时 session 都带 `timeout` 与
  `skip_auto_headers=("Content-Type",)`：token 的 `data=` 请求头与改动前 `requests`
  一致（不带 `Content-Type`），标签查询显式 `charset=gbk` / `Accept-Encoding`
  原样发出。
- **A1 不再阻塞事件循环**：把 IOP 请求打桩成 50ms 延迟，10 个并发
  `token2global()` 总耗时应接近 50ms（改动前用同步 `requests.post` 会串行成 ~500ms）。
- **C1 超时收口**：让 token / 标签查询抛 `asyncio.TimeoutError`，应看到
  `IOP token 请求失败` 或 `IOP 标签查询网络失败` 的 ERROR 日志、返回 `{}`，
  且 API 不 500；token 侧 `access_token` 被清空。
  把 `config.yaml` 的 IOP host/port 临时指向一个「accept 后不回数据」的本地端口
  可实测：默认 4 次 `sock_read=2` 触发约 8 秒，5 条 ERROR 日志，API 仍 200。
- **C2 生命周期**：`TestClient(app)` 作为上下文进入时
  `IOP.http_session.get_session()` 非 `None` 且 `closed=False`，退出后为 `None`
  （已 `close()`）；startup 日志含「IOP 共享 ClientSession 已创建」、
  shutdown 日志含「已关闭」，二者在同一 event loop。
- **C2 连接复用**：起本地 HTTP server 连打 5 次标签查询，服务端 accept 次数
  应为 1（keep-alive 复用）；每次新建 session 的旧实现为 5。
- **C2 降级**：不把 `TestClient` 当上下文管理器时 `get_session()` 为 `None`，
  首次请求打一条「降级为临时 session」WARNING（每进程只一次），结果仍正确、
  API 不 500，临时 session 用后即关闭。

## 未实现

- 基于 `version` 的进程级缓存、版本回滚、历史版本查询、灰度切换。
- 加工规则的完全配置化：当前 `cut_in_str2tuple` 与分组规则写在 `dim_transform.py`，
  `registry.json` 只声明列角色与列顺序。
