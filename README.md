# AIassistantCRM v5.0 · 项目说明

> **文档地图**：本文件是全局入口（在线业务 / 架构 / 降级总表 / 在线侧集成逻辑）。
> 三个可独立复用组件各有一份 `Readme.md`（用法、配置、流程、不变量与降级）：
> - [`IOP/Readme.md`](IOP/Readme.md)（运行参数改同目录的 `IOP/.env`）
> - [`local_excel2db/Readme.md`](local_excel2db/Readme.md)
> - [`sftp_avl2db/Readme.md`](sftp_avl2db/Readme.md)（运行参数改 `sftp_avl2db/.env`，产出落在包内 `runtime/`）

> 原 `Missions_writer.md`（488 行）与 `Missions_reader.md`（1098 行）已按职责拆分：三个包的说明进各自目录，在线业务与全局视角留在本文件。**那次拆分只整理文档**，代码、配置、行为均未改动。

> **职责边界**：各包 `Readme.md` 只描述组件自身（入口、配置、链路、约束）；
> `main.py` 如何调用与编排组件——IOP 客户端的进程级共享、按业务决定是否查询、
> 标签进入产品对象的路径——统一记在本文件「请求处理与路由」，不留在模块文档里。

本仓库由 **4 个进程**组成：2 个 `sftp_avl2db` 拉取任务、1 个 `local_excel2db` 维表写入、1 个 `main.py` 在线 API；在线侧对业务库与维表一律只读 SQLite（唯一写入是调用量统计 `log/call_sum.db`）。`sftp_avl2db/runtime/` 是写入侧产出的运行时数据目录，与配置和代码同在包内、用 `__file__` 定位，与启动 CWD 无关。库未就绪时 API 按「无业务记录 / 无维表」降级，不会 500。

## 架构与数据流向

```
sftp_avl2db/runtime/sqlite_db/{任务名}/*.db ──→ services/data_query.py ─┐
                                    ↑ PROFILES[...] + QUERY_SPECS 派发   ├─→ main.py（路由）→ services/（重组）→ output_script
dimensions.db ──→ reader.DimensionReader ──→ transform ─────────────────┘
```

> `sftp_avl2db/runtime/` 是**运行时数据目录**（写入侧产出：`log/` `state/`
> `files/` `sqlite_db/`），同目录还有唯一的 `.env`；在线侧只读这些库。
> 部署时不要整目录覆盖 `sftp_avl2db/`，可把 `runtime/` 做成指向数据盘的软链。

| 进程 | 数量 | 职责 |
|---|---|---|
| 写入侧进程 | 3 | 2 个 SFTP 拉取 + 1 个 `python -m local_excel2db`，见各包 `Readme.md` |
| 在线 API | 1 | 只读业务库与维表、按路由选业务对象并拼装话术；另在内存累计调用量并按阈值写入 `log/call_sum.db` |

**部署时 4 个进程都要起**。库未就绪时 API 按「无业务记录 / 无维表」降级，不会 500。
维表更新后无需重启在线进程，下一次请求即读到新值。

## 降级与容错（跨模块总表）

> 精简全局视角；**细目、触发条件与自检断言见各包 `Readme.md` 的对应章节**，本表不重复。

| 侧 | 触发 | 行为 | 细目 |
|---|---|---|---|
| 维表 `local_excel2db` | 库/表/列未就绪、配置异常、查询失败、注册 xlsx 缺失 | 读取侧返回 `{}`、写入侧跳过并保留上次版本，API 不 500 | [`local_excel2db/Readme.md`](local_excel2db/Readme.md)「读取侧与降级」 |
| 业务库 `sftp_avl2db` | 库不存在、`product_id` 未映射、查询异常 | `services/data_query.py` 返回 `{}`，spec 子类走空记录分支 | [`sftp_avl2db/Readme.md`](sftp_avl2db/Readme.md)「拉取不变量」 |
| IOP | token / 标签超时、连接失败、响应异常、失败冷却期 | 重试退避冷却后降级 `{}`，不阻塞其它并发请求 | [`IOP/Readme.md`](IOP/Readme.md)「并发与可靠性约束」 |
| 调用量库 | `log/call_sum.db` 写入 / 启动读取失败 | 保留 `_PENDING` 下轮重试，或基数按 0 恢复、服务照常启动 | 见下文「调用量统计」 |
| 在线 API | 任何上述依赖不可用 | 最终 `code: "2"` 空话术，不抛 500 | 见「请求处理与路由」 |

维表侧出任何问题都不会让 API 500；**不做进程级缓存**，每次调用读一次全表，天然读到写入侧的最新值，改库后下一次请求即生效、无需重启。

## 请求处理与路由（`main.py::marketAssistant`）

### 在线侧 IOP 集成（进程级客户端 → 请求级标签）

> 本节只记录 `main.py` 对 IOP 组件的**调用与编排**；IOP 客户端自身的入口、
> 配置、请求链路与降级契约见 [`IOP/Readme.md`](IOP/Readme.md)。

`main.py` 在模块级创建唯一的 `IOPClient`（`iop_client`），由 FastAPI `lifespan`
的 startup / shutdown 分别 `await iop_client.start()` / `await iop_client.aclose()`。
**进程内共享的是客户端**：token 状态与 `aiohttp` 会话由它自持，避免每请求重建
连接、重复刷新 token；**标签结果不共享**，仍按请求隔离（见步骤 1 与下文
「请求内隔离」）。

是否发起 IOP 查询由 `iop_need(product_id)` 逐 ID 声明（`NONE` / `PORTRAIT` /
`RAW_LABELS`）；`IopLabelProvider` 在请求内惰性取一次标签，保证一次请求最多查
一次、结果在 `productId` 循环内被各产品对象复用。`get_customer_labels()` 的
返回值（任何失败统一降级为 `{}`）作为 `custom_label_info` 传入业务对象，是
`ProductInfoRaw` 拼装 `queried_label_nvs` 与 `auxiliary_portrait` 的原始标签
输入；声明 `NONE` 的分支直接传 `{}`，连查询都不发生。

在此前提下，请求内的编排步骤为：

1. 在请求处理函数内创建 `IopLabelProvider`（标签惰性提供者）与空的
   `portrait_cache`；二者都是函数局部变量，不跨请求共享。
2. 卖点维表在循环外读入 1 次，作为同一份 dict 显式传给每个产品对象。
3. 按 `productId` 列表逐个建对象：先由 `iop_need(product_id)` 声明该 ID 对
   IOP 标签的依赖，非 `NONE` 时经提供者取标签（整请求最多 1 次），
   再按路由命中即 `continue`（顺序不可调换）：

| 顺序 | 条件（ID 来自配置） | 数据源 | 业务对象 |
|---|---|---|---|
| 1 | `OPPORTUNITY_RECALL_PRODUCT_IDS`（202608041615335173 / 172 / 174） | `data_query.get_opportunity_recall_latest(telnum, product_id)` | `ProductInfoBySpec9554` |
| 2 | `LOSS_CONTROL_PRODUCT_IDS`（202609180955095402） | `data_query.get_loss_control_latest(telnum)`，有记录才命中 | `ProductInfoBySpec9591` |
| 3 | 其余全部 | —（新线待完成） | `ProductInfoRaw` |

前两组 ID 不再硬编码在 `main.py`，分别由
`QUERY_SPECS["opportunity_recall"]["db_map"]` 的 key 与
`QUERY_SPECS["loss_control"]["product_ids"]` 派生。

第 3 类是唯一的兜底：不属于商机回捞 / 控损的产品 ID 全部落到这里，当前统一
输出 `code: "2"` 空话术，等客户诊断新线实现后再切换。维表业务路由已下线，
卖点表读取不再参与路由判断，只用于产品名与非商机 ID 的标签子集。

**按业务声明 IOP / 画像依赖**（`main.py::iop_need()`，由 `data_query` 的路由集合
派生；判定按全部 `productId` 取并集，不短路到第一个）：

| 业务 | 声明 | IOP 标签 | 画像 |
|---|---|---|---|
| 商机回捞 3 个 ID | `PORTRAIT` | 取 | 取，按 `product_id` 在一次请求内复用 |
| 控损 | `NONE` | **不取** | 不计算 |
| 其余全部（诊断兜底） | `RAW_LABELS` | 取（当前输出未消费，属新线预留） | 不计算 |

一次请求内的次数（不含 token 刷新，刷新次数另见 [`IOP/Readme.md`](IOP/Readme.md) 的「并发与可靠性约束 → token 单飞」）：

| 待查 `productId` 列表 | IOP 标签查询 | 画像拼装 | 卖点表读取 |
|---|---|---|---|
| 纯控损 | **0** | **0** | 1 |
| 纯商机 / 纯兜底 / 控损+商机 / 控损+兜底 / 三混合 | 1 | 商机不同 ID 数 | 1 |
| 3 个不同商机 | 1 | 3 | 1 |
| 同一商机 ID 重复 3 次 | 1 | 1 | 1 |

`custom_label_info` 与 `portrait_cache` 都只在 `marketAssistant` 函数体内创建：
标签属于单个手机号，放模块级或 `app.state` 缓存会把上一个用户的标签带给
下一个用户，不报错、只在话术里悄悄错。

4. 输出：仅 `ProductInfoBySpec9554` / `ProductInfoBySpec9591` 返回 `code: "0"`
   带话术；兜底返回 `code: "2"`、产品名与话术为空。这里不能靠 `product_name`
   是否为空判定：维表命中的 ID 落到兜底后 `product_name` 仍会从维表取到非空值，
   只看它会返回 `code: "0"` + 空话术，看起来成功、实际没有内容。

## 公共画像拼装（`services/product_info_raw.py`）

所有业务对象都继承 `ProductInfoRaw`。构造时完成两项：

- `queried_label_ids`：商机回捞的 3 个 ID 取代码内固定集（号卡 / 套餐共用
  3 个标签、宽带 4 个，两组重叠 2 个、合计 5 个；元组顺序即画像拼接顺序）；
  其余 ID 从卖点维表元组的 `[2]` 经 `.split("\n")` 得到（读取侧不提前拆、
  不过滤空串、不额外 strip 每项）。
- `queried_label_nvs`：按 `queried_label_ids` 顺序与 `custom_label_info` 对齐，缺失填 `None`。

`auxiliary_portrait` 是 **property，懒执行**：模块级纯函数
`build_auxiliary_portrait(queried_label_nvs)` 按上游返回的名称与原值逐项拼
`标签名: <strong><span style="color:blue;">值</span></strong>; `，保留空值跳过
与同名去重，最后补 `【辅助画像】` 前缀与 `rstrip`。固定标签集里的 5 个标签
都不涉及名称映射或按标签 ID 硬编码的值转换，这两类规则已一并取消。

只有声明 `PORTRAIT` 的商机对象会拿到 `portrait_cache`（`main.py` 在请求内
创建的空 dict），按 `product_id` 查/算并写入；控损与诊断兜底拿到 `None`，
property 直接返回 `""`，**连 `build_auxiliary_portrait()` 都不进**。`__str__`
仍打印 `auxiliary_portrait` 一行，这两类业务该行为空字符串。缓存只在函数内
创建、不跨请求共享，同一商机 ID 在一次请求里重复出现也只拼一次。

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

### 调用量库容错（原文细目）

| 侧 | 触发 | 行为 |
|---|---|---|
| 调用量库 | `log/call_sum.db` 写入失败 | 保留 `_PENDING` 增量并告警、下轮重试；`_DAY_TOTAL` 仍准确，不影响请求 |
| 调用量库 | 启动时读库失败 | 只告警、基数按 0 恢复，服务照常启动；库恢复后下次重启从真实值继续 |

## 自检要点（在线业务 / 路由）

- `import main` 正常，`app.router.on_startup` 为空（无定时任务）。
- 路由只有三类：商机 3 个 ID 走 `ProductInfoBySpec9554`、控损 ID 有记录时走
  `ProductInfoBySpec9591`、其余全部落 `ProductInfoRaw` 并返回 `code: "2"`
  空话术（产品名与话术均为空）。把维表里存在的业务 ID 传进去也应是 `code: "2"`，
  不能因为 `product_name` 非空而返回 `code: "0"` + 空话术。

- **按业务声明 IOP / 画像依赖**：`iop_need()` 对商机 3 个 ID 返回
  `PORTRAIT`、控损返回 `NONE`、其余（含维表里存在的业务 ID）返回
  `RAW_LABELS`；判定按全部 `productId` 取并集，把控损 ID 排在商机之前也不能
  漏查。

- **画像次数**：打桩 `build_auxiliary_portrait()`，控损 / 兜底 / 维表 ID 落兜底
  应为 **0** 次；纯商机 **1** 次；3 个不同商机 **3** 次；同一商机 ID 重复 3 次
  仍 **1** 次；混合请求等于该请求里不同的商机 `product_id` 数。
- **请求内隔离**：`IopLabelProvider` 与 `portrait_cache` 都在 `marketAssistant`
  函数体内创建，模块级 / `app.state` 不应有标签或画像缓存；连续两个不同手机号
  请求，响应互不串标签值、画像缓存也不串。提供者同实例第二次 `labels()`
  不再发起查询、异常仍降级 `{}` 且不抛 500。

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

## 未实现（在线业务）

- **客户诊断新线（暂不实现）**：`output_script` 计划由多张维表结合 IOP 用户画像
  拼装，不再依赖每日 AVL 宽表入库。这些表的结构与获取形式尚未确定，暂不在项目中
  实现，也不建代码占位或空模块；旧业务线已分步清理完毕，本项目不再保留其
  代码、配置、文档或样例。维表业务下线后，不属于商机回捞 / 控损的产品 ID
  全部作为诊断兜底，当前统一输出 `code: "2"` 空话术，等新线实现后再切换；
  维表库、`registry.json`、`reader.py` / `transform.py` 的加工能力均保留供复用。
  诊断兜底声明 `RAW_LABELS`、一定触发 IOP；当前输出尚未消费标签，属已确认的
  预留，不是遗漏的浪费。
- 按业务声明 IOP / 画像依赖已落地，不再是待办：三态声明、请求级惰性标签提供者、
  画像按 `product_id` 懒执行缓存、卖点表每请求 1 次，见「请求处理与路由」
  「公共画像拼装」与「自检要点」。

- 另：根目录 `test_online.py` 的请求体里仍写有 `18866816199`，
  属在线接口测试脚本，不在本次调试脚本范围内。
