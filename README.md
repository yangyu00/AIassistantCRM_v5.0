# AIassistantCRM v5.0 · 项目说明

> **文档地图**：本文件是全局入口（在线业务 / 架构 / 降级总表 / 在线侧集成逻辑）。
> 四个可独立复用组件各有一份说明文件（用法、配置、流程、不变量与降级）：
> - [`IOP/README.md`](IOP/README.md)（运行参数改同目录的 `IOP/.env`）
> - [`local_excel2db/README.md`](local_excel2db/README.md)（写入侧：配置与产物都在包内，运行时产物落在 `local_excel2db/runtime/`）
> - [`sftp_avl2db/README.md`](sftp_avl2db/README.md)（运行参数改 `sftp_avl2db/.env`，产出落在包内 `runtime/`）
> - [`call_stat/Readme.md`](call_stat/Readme.md)（运行参数改 `call_stat/.env`，库与 CSV 落在包内 `runtime/`，可 `python -m call_stat` 独立调用）
>
> 在线业务层单独一份说明：
> - [`services/README.md`](services/README.md)（四类路由、三类业务话术对象、**独立维护的读取配置**与降级；**不是可独立复用组件**）
>
> 另有两份随仓库提交的辅助说明（`tests/`、`io_samples/` 不是可独立复用组件）：
> - [`tests/README.md`](tests/README.md)（手工冒烟脚本：requests 打在线接口 + IOP 直连，**不是 pytest 单元测试**）
> - [`io_samples/README.md`](io_samples/README.md)（AVL / IOP 响应 / 维表 xlsx 样例；**`local_excel2db` 唯一输入是项目根 `excels/`**，样例只作格式对照）

> 各包的说明进各自目录，在线业务与全局视角留在本文件。

> 在线 API 的运行参数（端口、路由、日志目录）内联在 `main.py` 顶部常量区；四个组件的配置与运行时产物都锚在各自包内（`.env` / `registry.json` / `runtime/`），按 `__file__` 定位、与启动 CWD 无关。在线业务侧读哪些库、库里有哪些列由 `services/db_config.py` / `services/.env` 独立声明，不引用写入侧配置。

> 第四个可独立复用组件 `call_stat/` 提供在线调用量统计，配置与产物均内聚在包目录内，见下文「调用量统计接入」。

> **职责边界**：四个可独立复用组件的说明只描述组件自身（入口、配置、链路、约束）；
> 在线业务层的分层、四类路由与对外接口记在
> [`services/README.md`](services/README.md)；`main.py` 如何调用与编排组件——
> IOP 客户端的进程级共享、按业务决定是否查标签、标签进入产品对象的路径——
> 统一记在本文件「请求处理与路由」。

> 诊断新线 8 个 AVL（`F09627`～`F09633` / `F09635`）注册在 `sftp_avl2db`，
> 在线链路包含多行取数、场景交集与话术拼装、`services/routing.py` 诊断路由。
> 其中 `F09633`（用户场景对应关系）是唯一带 `product_no` 的表，千万级行，
> 按手机号分 128 张表；分表键与在线查询键一致，一次请求只命中一张分表，
> 分表与重灌口径见 [`sftp_avl2db/README.md`](sftp_avl2db/README.md)。
> 生产拉起时按「一个 AVL 一个进程」另起 8 个 `sftp_avl2db` 任务，
> 不计入下述当前部署的 6 个进程。实现说明见
> [`诊断新线_在线链路实现说明.md`](诊断新线_在线链路实现说明.md)，
> 注册详情见 [`sftp_avl2db/README.md`](sftp_avl2db/README.md)。

本仓库由 **6 个进程**组成：4 个 `sftp_avl2db` 拉取任务（每个 AVL 一个 db、一个进程）、1 个 `local_excel2db` 卖点维表写入、1 个 `main.py` 在线 API；在线侧只读 `sftp_avl2db` 业务库，并**每请求只读一次** `local_excel2db` 产出的 `dimensions.db` 取标签 ID 子集，唯一写入是调用量统计 `call_stat/runtime/call_sum.db`。`call_stat/` 是在线进程内的自包含模块：配置、代码、产物、说明都在包目录内，按 `__file__` 定位，与启动 CWD 无关；`sftp_avl2db/runtime/` 与 `local_excel2db/runtime/` 同理是写入侧产出的运行时数据目录。业务库未就绪时 API 按「无业务记录」降级，卖点维表未就绪时商机辅助画像为空，都不会 500。

## 架构与数据流向

```
写入侧：sftp_avl2db/runtime/sqlite_db/{项目名}/{AVL}.db
              │
              │（只读；在线侧按 services/db_config.py 自己的配置定位文件）
              ↓
在线侧：services/reader.py ──→ services/routing.py（四类路由）──→ 产品对象 ──→ main.py（响应组装）
              │                       │
              │                       ├─ 商机 / 控损：query() 单条 → opportunity_recall / loss_control
              │                       └─ 诊断新线：query_many() 多行 / 多条件
              │                            └─→ diagnosis/（7 表关联、场景交集、按位置配对）
              │                                 └─ 任一环节失败 → diagnosis/fallback.py 四段兜底
              └─ 只收 AVL 前缀；product_id → AVL 映射在 routing.py，诊断按 F09629 动态命中

卖点维表：项目根 excels/*.xlsx ──→ 写入进程 ──→ local_excel2db/runtime/sqlite_db/dimensions.db
                                                        │
                                                        │（在线侧 services/dimensions.py 每请求只读一次）
                                                        ↓
                       ┌─ selling_points.标签ID ───────────────→ 商机辅助画像
                       └─ 产品名称 / 匹配结果2 / 标签N ───────→ 诊断四段兜底
```

> `sftp_avl2db/runtime/` 与 `local_excel2db/runtime/` 都是**运行时数据目录**
> （写入侧产出：`sqlite_db/` `state/` `log/`，前者另有 `files/`），配置在各自包内
> （`.env` / `registry.json`），全部按 `__file__` 定位、与 CWD 无关。
> 在线侧只读磁盘上的业务库；库路径、表字段、查询键与分表数由
> `services/db_config.py` / `services/.env` **独立声明**，不引用写入侧配置；
> 库内表名恒等于 AVL 名（分表为 `{AVL}_{分表号}`）。
> `local_excel2db` 的输入固定为项目根 `excels/`（`source` 只允许纯文件名），
> 产物固定落在包内 `runtime/`，registry 与 CLI 都不提供路径覆盖；在线侧经
> `services/dimensions.py` 以 `mode=ro` 只读 `dimensions.db`，表名 / 列名取自
> `services/db_config.py` 的静态声明（**不新增进程、不做进程级缓存**）。
> 两侧唯一的交集是磁盘上的 db 文件，改列 / 改分表数 / 改路径须同步两侧声明，
> 见[解耦改造说明](解耦改造说明.md)。
> 部署时不要整目录覆盖这两个包，可把各自的 `runtime/` 做成指向数据盘的软链。

| 进程 | 数量 | 职责 |
|---|---|---|
| 写入侧进程 | 5 | 4 个 SFTP 拉取（每个 AVL 一个）+ 1 个 `python -m local_excel2db`，见各包说明 |
| 在线 API | 1 | 只读业务库 + 每请求批量读一次卖点维表（商机标签 ID / 诊断兜底），按路由选业务对象并拼装话术；另由 `call_stat` 模块在内存累计调用量并按阈值写入 `call_stat/runtime/call_sum.db` |

**部署时 6 个进程都要起**（`local_excel2db` 负责维护卖点维表库，在线侧读它取商机标签 ID 与诊断兜底内容）。业务库未就绪时诊断走卖点维表兜底（四段话术 `code: "0"`）；卖点维表库缺失 / 表结构漂移 / `product_id` 未命中时，商机辅助画像为空、诊断无法兜底（维持 `code: "2"`），都不会 500。`sftp_avl2db/.env` 中 `SFTP_PASSWORD` 为占位值，部署前须填真实密码；`IOP/.env` 含明文凭据，按当前约定随仓库入库，部署时按实际环境替换，对外分发前请评估凭据暴露风险。

## 降级与容错（跨模块总表）

> 精简全局视角；**细目、触发条件与自检断言见各包 `Readme.md` 的对应章节**，本表不重复。

| 侧 | 触发 | 行为 | 细目 |
|---|---|---|---|
| 维表写入 `local_excel2db` | 注册 xlsx 缺失、缺必需表头列、表结构漂移 | 写入侧告警跳过本表、保留上次成功版本，下轮重试；在线侧下次读到的仍是上次成功版本 | [`local_excel2db/README.md`](local_excel2db/README.md)「容错与日志」 |
| 卖点维表（在线读取侧） | 库不存在、表 / 必需列缺失、查询异常、`product_id` 未命中、标签 ID 为空 | [`services/dimensions.py`](services/README.md) 降级返回 `{}` / 空子集；商机辅助画像为空，诊断无法兜底（维持 `code: "2"`），API 不 500 | [`services/README.md`](services/README.md)「卖点维表读取侧」 |
| 诊断 7 表关联 | 任一查询返回空、IOP 返回里没有场景标签、场景交集为空、六项输出全空 | [`services/diagnosis/fallback.py`](services/README.md) 用卖点维表兜底：产品名按【】拆资费名称 / 摘要，「匹配结果2」顺序遍历命中 IOP 后取对应「标签N」的切入 / 拒绝原因 / 挽留话术，输出**四段**（无痛点诊断 / 营销话术），`code: "0"` | [`诊断新线_在线链路实现说明.md`](诊断新线_在线链路实现说明.md) 2.1 |
| 业务库（在线读取侧） | 库不存在、AVL 未注册、查询异常 | [`services/reader.py`](services/README.md) 返回 `{}` / `[]`；诊断侧据此触发上一条兜底 | [`services/README.md`](services/README.md)「读取侧」 |
| IOP | token / 标签超时、连接失败、响应异常、失败冷却期 | 重试退避冷却后降级 `{}`，不阻塞其它并发请求 | [`IOP/README.md`](IOP/README.md)「并发与可靠性约束」 |
| 调用量库 | `call_stat/runtime/call_sum.db` 写入 / 启动读取失败 | 保留 `_PENDING` 下轮重试，或基数按 0 恢复、服务照常启动 | [`call_stat/Readme.md`](call_stat/Readme.md)「降级与容错」 |
| 在线 API | 任何上述依赖不可用 | 最终 `code: "2"` 空话术，不抛 500 | 见「请求处理与路由」 |

在线侧**每次请求**只读一次 `dimensions.db`（`mode=ro`、批量 `IN`），不做进程级缓存：写入侧按 600s 轮询整表覆盖，缓存会让新维表不生效。`dimensions.db` 只由 `python -m local_excel2db` 独立进程维护；同一份映射同时供商机画像与诊断兜底使用，维表侧出任何问题都不会让 API 500。

## 请求处理与路由（`services/routing.py` + `main.py::marketAssistant`）

> 四类路由、`product_id → AVL` 映射与产品对象工厂都在 `services/routing.py`，
> 分层与接口见 [`services/README.md`](services/README.md)。本节记录 `main.py`
> 对 IOP 组件与业务层的**请求级编排**。

### 在线侧 IOP 集成（进程级客户端 → 请求级标签）

> 本节只记录 `main.py` 对 IOP 组件的**调用与编排**；IOP 客户端自身的入口、
> 配置、请求链路与降级契约见 [`IOP/README.md`](IOP/README.md)。

`main.py` 在模块级创建唯一的 `IOPClient`（`iop_client`），由 FastAPI `lifespan`
的 startup / shutdown 分别 `await iop_client.start()` / `await iop_client.aclose()`。
**进程内共享的是客户端**：token 状态与 `aiohttp` 会话由它自持，避免每请求重建
连接、重复刷新 token；**标签结果不共享**，仍按请求隔离（见步骤 1 与下文
「请求内隔离」）。

是否发起 IOP 查询由 `services/routing.py::need_labels(product_id)` 逐 ID 声明
（控损不查，其余都查）；声明只回答「查不查标签」，不回答「拼不拼画像」。
`IopLabelProvider` 在请求内惰性取一次标签，保证一次请求最多查一次、结果在
`productId` 循环内被各产品对象复用。`get_customer_labels()` 的返回值（任何失败
统一降级为 `{}`）作为 `custom_label_info` 传给对象工厂：商机的
`AuxiliaryPortrait` 用它拼装 `queried_label_nvs` 与 `auxiliary_portrait`，
诊断取数用它判定兜底场景标签；不需要标签的分支直接传 `{}`，连查询都不发生。

在此前提下，请求内的编排步骤为：

1. 在请求处理函数内创建 `IopLabelProvider`（标签惰性提供者）；它是函数局部
   变量，不跨请求共享。画像由商机对象自己拼，编排层不建任何缓存。
2. 在循环外调一次 [`services.dimensions.load_selling_points()`](services/README.md)，
   按本次全部 `productId` 批量 `IN` 查 `selling_points` 表，得到
   `{product_id: SellingPointRow}`（核心是该行「标签ID」列拆出的 `label_ids`）；
   显式传给对象工厂，由工厂按业务只转发给真正消费的对象，避免一个对象读一次库。
3. 按 `productId` 列表逐个建对象：先由 `need_labels(product_id)` 判断是否
   需要 IOP 标签，需要时经提供者取标签（整请求最多 1 次），再调
   [`services.build_product_object()`](services/README.md) 完成路由判定、取数与
   对象构造（顺序不可调换）。工厂只收标签与维表行，不再接收画像缓存：

| 顺序 | 条件（ID 来自 services/routing.py 路由表） | 数据源 | 业务对象 |
|---|---|---|---|
| 1 | `OPPORTUNITY_RECALL_PRODUCT_IDS`（202608041615335173 / 172 / 174） | `reader.query(OPPORTUNITY_RECALL_AVLS[id], telnum)` | `ProductInfoByOpportunityRecall` |
| 2 | `LOSS_CONTROL_PRODUCT_IDS`（202609180955095402） | `reader.query(LOSS_CONTROL_AVLS[id], telnum)`，有记录才命中 | `ProductInfoByLossControl` |
| 3 | 诊断新线 | `services.diagnosis.build_diagnosis_record()`（7 表关联）；任一环节失败改用卖点维表兜底 | `ProductInfoByDiagnosis` |
| 4 | 其余全部 | — | `ProductInfoRaw` |

商机回捞 / 控损的 `product_id → AVL 前缀` 对应关系**集中在
`services/routing.py` 的 `OPPORTUNITY_RECALL_AVLS` / `LOSS_CONTROL_AVLS` 两张表**，
两个 ID 集合由它们的 key 派生。读取侧 `services/reader.py` 完全不知道产品 ID：
命中后把 AVL 前缀传给它即可，只按 `{AVL}.db` 定位。

**诊断新线不维护静态 product_id 映射**：`F09629`（`DIM_MARKET_PRODUCT_BUSINESS_ONCE`）
本身就是「产品ID → 策略 / 产品」的映射表；由 `services/diagnosis/query.py` 用
`query_many()` 关联另外 6 张维表。**任一查询返回空 / IOP 返回里没有场景标签 /
场景交集为空 / 六项全空** → 改用卖点维表兜底，仍返回 `ProductInfoByDiagnosis`
（`code: "0"`）与四段话术；连维表里也没有该 `product_id` 时才落第 4 类
`code: "2"`。取数与兜底规则见
[`诊断新线_在线链路实现说明.md`](诊断新线_在线链路实现说明.md) 2.1。
卖点维表**不参与路由判断**：四类路由仍由 `routing.py` 的静态表与业务归属决定。

**按业务声明 IOP 查询依赖**（`services/routing.py::need_labels()`，由该模块的
路由集合派生；判定按全部 `productId` 取并集，不短路到第一个）：

| 业务 | 是否查 IOP 标签 | 辅助画像 |
|---|---|---|
| 商机回捞 3 个 ID | 查 | 由商机对象的 `AuxiliaryPortrait` 拼装 |
| 控损 | **不查** | 无画像组件 |
| 诊断新线 / 兜底 | 查（正常链路不消费；兜底链路用它判定哪个场景标签 ID 命中） | 无画像组件 |

「是否生成画像」不再是一层需要传递的声明：只有商机对象持有画像组件，
维表未命中 / 标签子集为空 / IOP 降级为 `{}` 时 `build_auxiliary_portrait()`
自然返回空串，商机话术的 `if portrait` 就不追加。空输入即空输出，无需
显式开关，也不需要请求级画像缓存。

一次请求内的次数（不含 token 刷新，刷新次数另见 [`IOP/README.md`](IOP/README.md)
的「并发与可靠性约束 → token 单飞」）：

| 待查 `productId` 列表 | IOP 标签查询 | 辅助画像拼装 |
|---|---|---|
| 纯控损 | **0** | **0** |
| 纯商机 / 纯诊断 / 控损+商机 / 三混合 | 1 | 请求里的商机对象数（重复 ID 也各算 1 次） |
| 3 个不同商机 | 1 | 3 |
| 同一商机 ID 重复 3 次 | 1 | 3 |

画像拼装是零 I/O 的纯字符串拼接，结果只缓存在**单个商机对象实例**上
（话术拼装与日志各读一次，只算一遍）。原先按 `product_id` 跨对象复用的
请求级缓存已去掉：它不省任何数据库或 IOP 查询，只省一次可以忽略的字符串
拼接，却要求编排层、对象工厂和组件都多带一个可变状态。真要省重复 ID 的
开销，应在编排层对 `productId` 去重或缓存整个产品对象的输出，那是明确的
性能决策，不与「是否生成画像」混在一起。

卖点维表另有 **每请求 1 次**只读查询（与设计稿口径一致，不按产品对象重复读），
不随 `productId` 数量增长；它不计入上表的 IOP / 画像次数。库缺失时该次查询
降级为 `{}`，商机标签子集为空、辅助画像为空串。

`IopLabelProvider` 与 `custom_label_info` 都只在 `marketAssistant` 函数体内
创建：标签属于单个手机号，放模块级或 `app.state` 缓存会把上一个用户的标签
带给下一个用户，不报错、只在话术里悄悄错。画像结果只挂在当次请求的商机对象
实例上，随对象一起回收，同样不跨请求共享。

4. 输出：`ProductInfoByOpportunityRecall` / `ProductInfoByLossControl` /
   `ProductInfoByDiagnosis` 三类对象返回 `code: "0"` 带话术；兜底返回
   `code: "2"`、产品名与话术为空。
   这里按对象类型判定，不能靠 `product_name` 是否为空：兜底对象也可能因维表残留
   拿到非空名称，只看它会返回 `code: "0"` + 空话术，看起来成功、实际没有内容。
   诊断对象的 `productName` 取「资费名称」（【】内），正常链路 `output_script`
   是六段【资费名称 / 资费摘要 / 痛点诊断 / 切入话术 / 营销话术 / 挽留话术】HTML；
   **卖点维表兜底时省略【痛点诊断】【营销话术】，只输出四段**（两段没有维表来源，
   不是填「无」），资费名称 / 摘要来自维表「产品名称」按【】拆分，切入话术与
   拒绝原因 / 挽留话术来自「匹配结果2」首个命中 IOP 的标签 ID 对应的「标签N」。

## 对象模型与辅助画像（`services/product_info_raw.py` + `services/dimensions.py`）

四类对象**只继承极简的 `ProductInfoBase`**（`product_id` / `product_name` /
`output_script` + 统一日志头），业务对象各自负责自己的名称与话术；公共字段
日志由基类统一输出，子类通过 `_extra_str_lines()` 追加自己的行。

- `AuxiliaryPortrait`：**商机专属组件**（组合，不是继承）。持有 IOP
  `custom_label_info` 与卖点维表行映射 `selling_point_rows`（旧名
  `product_selling_point_match_dict` 已废弃），画像结果缓存在实例上。
- 控损 / 诊断新线 / 兜底对象**没有任何画像相关属性**：`routing.build_product_object()`
  按分支只传各自消费的参数。控损只收 `record`；诊断的 IOP 标签与维表行在
  `build_diagnosis_record()` 取数阶段用完，对象只收结构化 `record`；兜底只收
  ID 与维表行（用于日志产品名）。
- `ProductInfoRaw` 现在只表示第 4 类兜底对象，不再兼任公共基类。

**标签 ID 子集不再硬编码**：在线侧每请求从卖点维表 `selling_points` 表按
`product_id` 查该行「标签ID」列（`services/db_config.py` 声明为 `label_id`），拆成保序
去重的 ID 元组；号码对应的名称与值仍来自 IOP 返回。维表决定「查哪些 ID」，
IOP 决定「这些 ID 叫什么、值是多少」，两者职责分开。「标签ID」单元格按换行 /
竖线 / 逗号 / 分号（含全角）拆分、去空、保序去重，见
`services/dimensions.py::split_label_ids()`。

`queried_label_ids` / `queried_label_nvs` / `auxiliary_portrait` 都是组件上的
懒执行 property：模块级纯函数 `build_auxiliary_portrait(queried_label_nvs)`
按上游返回的名称与原值逐项拼
`标签名: <strong><span style="color:blue;">值</span></strong>; `，跳过空值，
最后补 `【辅助画像】` 前缀与 `rstrip`。不做名称映射或按标签 ID 硬编码的值转换。

`build_auxiliary_portrait()` 是全链路唯一的画像入口，也是唯一的「不生成」
判定：空列表或全空值直接返回 `""`，商机话术里 `if portrait` 就不追加这一段。
只有商机对象会创建 `AuxiliaryPortrait`，控损 / 诊断 / 兜底连组件都没有；
结果只缓存在当前对象实例上，话术拼装与日志各读一次只算一遍，不跨对象、
不跨请求共享，编排层也不需要传任何缓存进来。

## 调用量统计接入（`call_stat/`）

调用量统计由独立模块 `call_stat/` 提供：配置唯一来源是 `call_stat/.env`，库与 CSV 恒落在 `call_stat/runtime/`。组件自身的入口、配置、链路、不变量、降级、部署与自检全部记在 [`call_stat/Readme.md`](call_stat/Readme.md)；在线侧运行参数（端口、路由、日志目录）集中在 `main.py` 顶部。卖点维表同理：写入侧配置锚在 `local_excel2db/registry.json`、产物锚在 `local_excel2db/runtime/`，输入固定为项目根 `excels/`；在线只读侧在 `services/dimensions.py`，库路径与列声明由 `services/db_config.py` 独立维护。

集成层只做三件事（都在 `main.py`）：

| 阶段 | 动作 |
|---|---|
| 模块级 | `app.middleware("http")(count_call_middleware)` 注册每次访问计数；`app.include_router(build_router())` 注册 GET 查询路由（路径取自 `CALL_STAT_QUERY_PATH`） |
| lifespan startup | `startup()` 建表并从库恢复当天基数，日志打印「调用量统计已恢复：当天已累计 N 次」 |
| lifespan shutdown | `flush()` 补齐不足阈值的尾部增量 |

```python
from call_stat import startup, flush, count_call_middleware, build_router
```

`uvicorn` 的 SIGTERM 退出路径不经过 `atexit`，所以 lifespan 的 `finally` 必须显式 `flush()`；模块内 `atexit` 仍作非 ASGI 运行方式的兜底，两处幂等，钩子挂在 `router.lifespan_context`（`app.router.on_startup` / `on_shutdown` 均为空）。

查询与独立调用：

```bash
curl "http://host:8989/api/callStat?date=2026-11-16"
curl "http://host:8989/api/callStat?month=2026-11"
python -m call_stat --date 2026-11-16
python -m call_stat --month 2026-11 --export     # CSV 落在 call_stat/runtime/export/
python -m call_stat --self-check
```

## 自检要点（在线业务 / 路由）

- `import main` 正常，`app.router.on_startup` 为空（无定时任务）。
- 路由四类：商机 3 个 ID 走 `ProductInfoByOpportunityRecall`、控损 ID 有记录时走
  `ProductInfoByLossControl`、诊断新线走 `ProductInfoByDiagnosis`
  （`code: "0"` + 六段话术；任一环节失败走卖点维表兜底，仍 `code: "0"` + 四段话术），
  其余落 `ProductInfoRaw` 并返回 `code: "2"` 空话术（响应里 `productName` 与话术均为空）。
  商机标签 ID 子集来自卖点维表，维表未命中时辅助画像为空；`code` 始终按对象类型判定，
  不会因 `product_name` 非空而误判为 `code: "0"`。
- **诊断新线一键自检**：`python tests/check_diagnosis_offline.py`（零第三方依赖主体，
  不连 SFTP / IOP、不写包内 `runtime/`）。用 8 个样例 AVL 建库 + 补一组业务键
  自洽数据，断言括号拆分、多值按位置配对、场景交集汇总、RN 优先级、多行话术、
  挽留配对、**单一 flat 解析器**（商机与诊断共用、末列不转 int、主键下标按注册
  换算）、**业务包按业务名命名（无历史别名、商机 / 控损重构前后输出等价）**、
  `query()` 单条查询回归与 `main.py` 三类路由输出，结论应为 `ALL PASS`。

- **卖点维表 → 商机画像自检**（同一脚本 1d 节）：按 `services/db_config.py` 的
  `SELLING_POINTS_*` 声明建临时 `dimensions.db`，插 3 行商机 ID；断言换行 / 竖线
  分隔的「标签ID」列被拆成保序去重元组、商机 `queried_label_ids` 与
  `auxiliary_portrait` 来自维表（用与旧固定集完全不同的 ID 验证）、库缺失与
  未命中降级为空画像且不抛异常；在线路由段再用 IOP 桩端到端验证
  「维表选 ID → IOP 取名值 → 辅助画像」，诊断对象不带画像。

- **诊断卖点维表兜底自检**（同一脚本 1e / 5b 节与在线路由第 7 段）：断言
  「标签N」单元格按 `切入话术` / `拒绝挽留话术` 解析（含去 `**`）、「匹配结果2」
  保序不去重且与 `tag_*` 同下标、**按顺序遍历命中第 2 个 ID 即中断并取 tag1**；
  db 查空 / 场景交集为空 / IOP 无场景标签三种触发都得到兜底 record，产品名按【】
  拆出资费名称 / 摘要，输出**四段**（无【痛点诊断】【营销话术】）；无 ID 命中时
  切入 / 挽留填「无」，维表里也没有该 `product_id` 才落 `code: "2"`。在线路由段
  用 IOP 桩验证 db 查空时仍返回 `code=0` 的四段兜底话术。

- **`F09633` 分表自检**（同一脚本 1c 节）：建满 128 张 `F09633_{i}`、样例号码只落在
  `crc32(product_no) % 128` 指定的那一张、`reader.query_many(where={"product_no": telnum})`
  能精确定位、缺 `product_no` 时降级 `[]` 而不广播。分表数在两侧各自声明
  （`sftp_avl2db/.env` 与 `services/.env` 的 `F09633_NUM_TABLES`），自检会断言
  两侧分表口径一致。

- **按业务声明 IOP 查询依赖**：`need_labels()` 对控损 ID 返回 `False`、其余
  （含商机与维表里存在的业务 ID）返回 `True`；判定按全部 `productId` 取并集，
  把控损 ID 排在商机之前也不能漏查。控损请求不应发起任何 IOP 标签查询。

- **画像归属**：控损 / 诊断 / 兜底对象不持有 `portrait`、`_portrait`、
  `custom_label_info`、`selling_point_rows` 与画像相关 property；只有商机对象
  拼画像。维表未命中或 IOP 降级为 `{}` 时 `auxiliary_portrait` 为空串，
  `output_script` 不含 `【辅助画像】`，不需要显式开关。
- **请求内隔离**：`IopLabelProvider` 与 `custom_label_info` 都在
  `marketAssistant` 函数体内创建，模块级 / `app.state` 不应有标签缓存；画像
  结果只挂在当次请求的商机对象实例上，随对象回收。连续两个不同手机号请求，
  响应互不串标签值、也不串画像。提供者同实例第二次 `labels()` 不再发起查询、
  异常仍降级 `{}` 且不抛 500。

- `app.router.on_startup` / `on_shutdown` 均为 0，`router.lifespan_context` 已设置
  （确认走 lifespan 而非 `on_event`）；startup 日志有「调用量统计已恢复」。
- **调用量统计自检**：把 `call_stat/.env` 的 `CALL_STAT_FLUSH_THRESHOLD` 设为 3
  （或启动时 `configure(flush_threshold=3)`）：连续 3 次请求后才写一次
  `call_stat/runtime/call_sum.db`；第 1、2 次时 `GET /api/callStat?date=今天` 的
  `callCount` 已含内存增量；`flush()` 后 `_PENDING` 为空；GET 不传/传两个参数、
  `date=2026-1-6`、`month=2026-13` 均返回 `res_code: 1`。**重启继续计数**、
  完整断言与一键命令见 [`call_stat/Readme.md`](call_stat/Readme.md)「自检」
  （`python -m call_stat --self-check`）。
- 起 uvicorn 打几个不足阈值的请求后 `kill -TERM`：日志出现
  `Application shutdown complete.`，且 `call_stat/runtime/call_sum.db` 里含这几条
  尾部增量（验证 lifespan shutdown，而非只依赖 `atexit`）。

## 客户诊断新线与待确认项

8 张表注册（`sftp_avl2db`）、多行 / 多条件查询（`query_many` / `query_in`）、
7 表关联取数、场景交集与话术拼装（`services/diagnosis/`）、
**卖点维表兜底**（`diagnosis/fallback.py`）、`services/routing.py` 诊断路由与
`code: "0"` 输出。资费名称 / 资费摘要取 `F09629.BUSINESS_NAME` 按【】内外拆分；
兜底时改取卖点维表「产品名称」按同一规则拆分。`F09633` 是千万级用户场景表，
按 `product_no` 分 128 张表（分表数由 `F09633_NUM_TABLES` 配置；改这个数必须
重灌全量 AVL，不存在历史单表迁移路径），见
[`sftp_avl2db/README.md`](sftp_avl2db/README.md)「F09633 分表」。
完整字段路径与验证见
[`诊断新线_在线链路实现说明.md`](诊断新线_在线链路实现说明.md)，表注册见
[`sftp_avl2db/README.md`](sftp_avl2db/README.md)「诊断新线：8 张表注册」。

**待业务确认（不影响主链路运行，均集中在 `services/diagnosis/constants.py`）**：

- 单元格多值的「指定分隔符」：样例未出现多值，默认支持竖线 / 全角竖线 /
  单元格内换行；上游给出正式分隔符后改 `MULTI_VALUE_SPLIT_RE`。
- `RN` 方向：按 rank 语义默认「RN 越小优先级越高」；若口径相反改
  `SMALLER_RN_HIGHER_PRIORITY`。
- `F09635`（标签ID-标签名）在取数说明里没有位置，未接入主链路；话术样例中的
  `#客运个性化运营标签34&` 一类占位符也未做替换（缺映射依据）。
- 诊断兜底口径：「任一查询空即整单兜底」比原来「单表为空只填『无』」更激进；
  「IOP 无场景标签」的判定集中在 `fallback.has_scene_labels()`；「标签N」分段
  解析集中在 `fallback.parse_tag_script()`，三处口径要改都只改一处。

**已实现**（见「请求处理与路由」「对象模型与辅助画像」与「自检要点」）：
按业务声明 IOP 查询依赖的布尔判定、请求级惰性标签提供者、辅助画像由商机
对象自行拼装并只缓存在实例上、卖点维表每请求只读 1 次并据此取商机标签 ID
子集与诊断兜底内容（产品名 /「匹配结果2」/「标签N」），诊断任一环节失败
输出四段兜底话术。

**待优化**：诊断取数目前按产品 ID 逐个查库，一次请求多个产品 ID 时可加请求内
缓存减少重复查询；若同一 `productId` 在请求里重复出现，也应在编排层去重或
缓存整个产品对象的输出，而不是在业务对象里按 ID 复用局部结果。

- [`tests/`](tests/README.md) 收录手工冒烟脚本（在线接口、入参校验、调用量统计、
  IOP 直连），端口与 `main.py` 一致为 8989；它们都是「跑一次、直接请求、打印返回」
  的脚本，不是 pytest 单元测试。
