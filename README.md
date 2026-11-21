# AIassistantCRM v5.0 · 项目说明

> **文档地图**：本文件是全局入口（在线业务 / 架构 / 降级总表 / 在线侧集成逻辑）。
> 四个可独立复用组件各有一份说明文件（用法、配置、流程、不变量与降级）：
> - [`IOP/README.md`](IOP/README.md)（运行参数改同目录的 `IOP/.env`）
> - [`local_excel2db/README.md`](local_excel2db/README.md)
> - [`sftp_avl2db/README.md`](sftp_avl2db/README.md)（运行参数改 `sftp_avl2db/.env`，产出落在包内 `runtime/`）
> - [`call_stat/Readme.md`](call_stat/Readme.md)（运行参数改 `call_stat/.env`，库与 CSV 落在包内 `runtime/`，可 `python -m call_stat` 独立调用）
> - [`tests/README.md`](tests/README.md)（手工冒烟脚本：requests 打在线接口 + IOP 直连，**不是 pytest 单元测试**）
> - [`io_samples/README.md`](io_samples/README.md)（AVL / IOP 响应 / 维表 xlsx 样例；**维表实际投放目录是 `excels/`**，不是 `io_samples/`）

> 各包的说明进各自目录，在线业务与全局视角留在本文件。

> 在线 API 的运行参数（端口、路由、日志目录）内联在 `main.py` 顶部常量区；四个组件的配置在各自包内的 `.env` / `registry.json`。

> 第四个可独立复用组件 `call_stat/` 提供在线调用量统计，配置与产物均内聚在包目录内，见下文「调用量统计接入」。

> **职责边界**：各包说明文件只描述组件自身（入口、配置、链路、约束）；
> `main.py` 如何调用与编排组件——IOP 客户端的进程级共享、按业务决定是否查询、
> 标签进入产品对象的路径——统一记在本文件「请求处理与路由」，不留在模块文档里。

本仓库由 **6 个进程**组成：4 个 `sftp_avl2db` 拉取任务（每个 AVL 一个 db、一个进程）、1 个 `local_excel2db` 维表写入、1 个 `main.py` 在线 API；在线侧对业务库与维表一律只读 SQLite（唯一写入是调用量统计 `call_stat/runtime/call_sum.db`）。`call_stat/` 是在线进程内的自包含模块：配置、代码、产物、说明都在包目录内，按 `__file__` 定位，与启动 CWD 无关；`sftp_avl2db/runtime/` 同理是写入侧产出的运行时数据目录。库未就绪时 API 按「无业务记录 / 无维表」降级，不会 500。

## 架构与数据流向

```
sftp_avl2db/runtime/sqlite_db/{项目名}/{AVL}.db ──→ sftp_avl2db/data_query.py ─┐
                                    ↑ 只收 AVL 前缀（product_id 留在 main.py） ├─→ main.py（路由）→ services/（重组）→ output_script
dimensions.db ──→ reader.DimensionReader ──→ transform ────────────────────┘
```

> `sftp_avl2db/runtime/` 是**运行时数据目录**（写入侧产出：`log/` `state/`
> `files/` `sqlite_db/`），同目录还有唯一的 `.env`；在线侧只读这些库。
> 库内表名恒等于 AVL 名（分表为 `{AVL}_{分表号}`）。
> 部署时不要整目录覆盖 `sftp_avl2db/`，可把 `runtime/` 做成指向数据盘的软链。

| 进程 | 数量 | 职责 |
|---|---|---|
| 写入侧进程 | 5 | 4 个 SFTP 拉取（每个 AVL 一个）+ 1 个 `python -m local_excel2db`，见各包说明 |
| 在线 API | 1 | 只读业务库与维表、按路由选业务对象并拼装话术；另由 `call_stat` 模块在内存累计调用量并按阈值写入 `call_stat/runtime/call_sum.db` |

**部署时 6 个进程都要起**。库未就绪时 API 按「无业务记录 / 无维表」降级，不会 500。
维表更新后无需重启在线进程，下一次请求即读到新值。`sftp_avl2db/.env` 中
`SFTP_PASSWORD` 为占位值，部署前须填真实密码；`IOP/.env` 含明文凭据，不应入库。

## 降级与容错（跨模块总表）

> 精简全局视角；**细目、触发条件与自检断言见各包 `Readme.md` 的对应章节**，本表不重复。

| 侧 | 触发 | 行为 | 细目 |
|---|---|---|---|
| 维表 `local_excel2db` | 库/表/列未就绪、配置异常、查询失败、注册 xlsx 缺失 | 读取侧返回 `{}`、写入侧跳过并保留上次版本，API 不 500 | [`local_excel2db/README.md`](local_excel2db/README.md)「读取侧与降级」 |
| 业务库 `sftp_avl2db` | 库不存在、AVL 未注册、查询异常 | `sftp_avl2db/data_query.py` 返回 `{}`，spec 子类走空记录分支 | [`sftp_avl2db/README.md`](sftp_avl2db/README.md)「拉取不变量」 |
| IOP | token / 标签超时、连接失败、响应异常、失败冷却期 | 重试退避冷却后降级 `{}`，不阻塞其它并发请求 | [`IOP/README.md`](IOP/README.md)「并发与可靠性约束」 |
| 调用量库 | `call_stat/runtime/call_sum.db` 写入 / 启动读取失败 | 保留 `_PENDING` 下轮重试，或基数按 0 恢复、服务照常启动 | [`call_stat/Readme.md`](call_stat/Readme.md)「降级与容错」 |
| 在线 API | 任何上述依赖不可用 | 最终 `code: "2"` 空话术，不抛 500 | 见「请求处理与路由」 |

维表侧出任何问题都不会让 API 500；**不做进程级缓存**，每次调用读一次全表，天然读到写入侧的最新值，改库后下一次请求即生效、无需重启。

## 请求处理与路由（`main.py::marketAssistant`）

### 在线侧 IOP 集成（进程级客户端 → 请求级标签）

> 本节只记录 `main.py` 对 IOP 组件的**调用与编排**；IOP 客户端自身的入口、
> 配置、请求链路与降级契约见 [`IOP/README.md`](IOP/README.md)。

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

| 顺序 | 条件（ID 来自 main.py 路由表） | 数据源 | 业务对象 |
|---|---|---|---|
| 1 | `OPPORTUNITY_RECALL_PRODUCT_IDS`（202608041615335173 / 172 / 174） | `data_query.query(OPPORTUNITY_RECALL_AVLS[id], telnum)` | `ProductInfoBySpec9554` |
| 2 | `LOSS_CONTROL_PRODUCT_IDS`（202609180955095402） | `data_query.query(LOSS_CONTROL_AVLS[id], telnum)`，有记录才命中 | `ProductInfoBySpec9591` |
| 3 | 其余全部 | —（新线待完成） | `ProductInfoRaw` |

`product_id → AVL 前缀` 的对应关系**集中在 `main.py` 顶部的
`OPPORTUNITY_RECALL_AVLS` / `LOSS_CONTROL_AVLS` 两张表**，两个 ID 集合由它们的 key
派生。数据访问层 `sftp_avl2db` 完全不知道产品 ID：命中后把 AVL 前缀传给
`data_query.query(avl, telnum)` 即可，读取侧只按 `{AVL}.db` 定位。

第 3 类是唯一的兜底：不属于商机回捞 / 控损的产品 ID 全部落到这里，当前统一
输出 `code: "2"` 空话术，等客户诊断新线实现后再切换。维表业务路由已下线，
卖点表读取不再参与路由判断，只用于产品名与非商机 ID 的标签子集。

**按业务声明 IOP / 画像依赖**（`main.py::iop_need()`，由本文件的路由集合
派生；判定按全部 `productId` 取并集，不短路到第一个）：

| 业务 | 声明 | IOP 标签 | 画像 |
|---|---|---|---|
| 商机回捞 3 个 ID | `PORTRAIT` | 取 | 取，按 `product_id` 在一次请求内复用 |
| 控损 | `NONE` | **不取** | 不计算 |
| 其余全部（诊断兜底） | `RAW_LABELS` | 取（当前输出未消费，属新线预留） | 不计算 |

一次请求内的次数（不含 token 刷新，刷新次数另见 [`IOP/README.md`](IOP/README.md) 的「并发与可靠性约束 → token 单飞」）：

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

## 调用量统计接入（`call_stat/`）

调用量统计由独立模块 `call_stat/` 提供：配置唯一来源是 `call_stat/.env`，库与 CSV 恒落在 `call_stat/runtime/`。组件自身的入口、配置、链路、不变量、降级、部署与自检全部记在 [`call_stat/Readme.md`](call_stat/Readme.md)；在线侧运行参数（端口、路由、日志目录）已内联在 `main.py` 顶部，仓库根目录不再有 `config.py` / `config.yaml`。

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

## 未实现（在线业务）

- **客户诊断新线（暂不实现）**：`output_script` 计划由多张维表结合 IOP 用户画像
  拼装。这些表的结构与获取形式尚未确定，暂不在项目中实现，也不建代码占位或
  空模块。不属于商机回捞 / 控损的产品 ID 全部作为诊断兜底，当前统一输出
  `code: "2"` 空话术，等新线实现后再切换；维表库、`registry.json`、
  `reader.py` / `transform.py` 的加工能力保留供复用。诊断兜底声明
  `RAW_LABELS`、一定触发 IOP；当前输出尚未消费标签，属已确认的预留。
- 按业务声明 IOP / 画像依赖：三态声明、请求级惰性标签提供者、画像按
  `product_id` 懒执行缓存、卖点表每请求 1 次，见「请求处理与路由」
  「公共画像拼装」与「自检要点」。

- [`tests/`](tests/README.md) 收录手工冒烟脚本（在线接口、入参校验、调用量统计、
  IOP 直连），端口与 `main.py` 一致为 8989；它们都是「跑一次、直接请求、打印返回」
  的脚本，不是 pytest 单元测试。
