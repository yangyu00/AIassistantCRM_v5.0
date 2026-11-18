# 在线业务读取与重组说明（FastAPI）

> 写入侧（SFTP 拉取 2 个进程、维表写入 1 个进程）见 `Missions_writer.md`。
> 在线 API（`python main.py`）无任何定时任务，只有一个 `lifespan` 生命周期钩子
> （启动时恢复调用量基数、关闭时落库，见「调用量统计」）；
> 业务库与维表一律只读 SQLite，唯一写入是调用量统计（本地 `log/call_sum.db`）。
> 两个任务的业务命名与配置键前缀见 `Missions_writer.md` 的「任务命名」。

## 架构与数据流向

```
tasks/sqlite_db/{任务名}/*.db ──→ services/data_query.py ───────┐
                                    ↑ 按 TASKS[...] + QUERY_SPECS 派发  ├─→ main.py（路由）→ services/（重组）→ output_script
dimensions.db ──→ dim_reader.py ──→ dim_transform ───────────────┘
```

| 进程 | 数量 | 职责 |
|---|---|---|
| 写入侧进程 | 3 | 2 个 SFTP 拉取 + 1 个 `dim_watcher`，见 `Missions_writer.md` |
| 在线 API | 1 | 只读业务库与维表、按路由选业务对象并拼装话术；另在内存累计调用量并按阈值写入 `log/call_sum.db` |

**部署时 4 个进程都要起**。库未就绪时 API 按「无业务记录 / 无维表」降级，不会 500。
维表更新后无需重启在线进程，下一次请求即读到新值。

## 请求处理与路由（`main.py::marketAssistant`）

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

一次请求内的次数（不含 token 刷新，刷新次数另见「Token 单飞」）：

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

## 用户标签获取（IOP）

`IOP/get_custinfo.py::get_custom_info_in_global(tel)` 由
`main.py::IopLabelProvider.labels()` 惰性调用：一次请求内最多调用一次，结果在
该请求的 `productId` 循环内复用；是否调用由各产品 ID 的 `iop_need()` 声明
决定（纯控损请求为 0 次，其余为 1 次）。返回
`{ITEMCODE: [ITEMNAME, ITEMVALUE], ...}`。提供者只在请求处理函数内实例化，
异常仍走 `my_log.error(e)` 并降级 `{}`，本请求内保持该结果、不再重试。
入口处先 `await ensure_token()`（token 单飞 + 重试/退避/冷却 + 响应校验 +
有效期换算，见后续四节），再发标签查询；token 刷新对同一批并发请求
只发生一次，持续失败时冷却期内的请求直接降级、不再打认证服务。

### 返回契约与降级

**所有失败分支统一返回空 dict `{}`**，不再返回 `None`。原因是 `main.py` 的
`try/except` 只包住函数调用本身，`None` 不抛异常、不会触发 `except`，会一路传到
`ProductInfoRaw::__get_queried_label_nvs` 并抛 `TypeError`（只要
`queried_label_ids` 非空），使"token 拿不到"这个最该降级的场景反而返回 500。空 dict 则让下游
按「无标签」正常产出话术。

失败分支包括：token 不可用、token 请求超时/网络异常、**token 响应非 200 /
解析失败 / 结构异常 / 缺字段 / 上游 `error`**（详见「Token 响应校验」），以及标签查询连接或读超时、
响应 JSON 解析失败、非 200、`res_code != 0`、响应非 dict、缺少或非列表的
`BUSIINFO`。缺 `ITEMCODE` / 非 dict 的单条记录跳过，不拖累整批。
`res_code` 按 `str(...)` 比较，兼容上游返回字符串 `"0"` 或数字 `0`。

token 侧任一失败都会清空 `TOKENS` 并记 ERROR，按配置重试/退避/冷却，
最终仍无有效 token 时统一返回 `{}`；**不会沿用可能已过期的旧 access_token**。

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

响应校验后 `IOP token 请求失败` 统一带 `status=` 与具体原因（状态码非 200 /
响应解析失败 / 响应结构异常 / `access_token` 为空 / 上游返回错误 / 缺少
`access_token` 且无 `error` / 网络异常）。网络异常发生在建连阶段时
`status=None`。前缀保持不变，只是把原先笼统的 `error=` 与 `str(res_data)`
换成结构化字段。

### 配置读取

`global_config = ImmutableYamlConfig()` 只在 `IOP/http_session.py` 里读一次，
`get_token.py` / `get_custinfo.py` 直接复用同一个只读对象。`ImmutableYamlConfig`
是单例且返回只读配置，但每次调用其构造函数都会重新 `open` + `yaml.load` 并
重建字典，原先每请求至少一次磁盘 I/O + yaml 解析（token 刷新时再多一次）。
现在请求路径为 0 次，IOP 侧 import 阶段的读盘也由两次收敛为一次。
运行期修改 `config.yaml` 不会热生效，需重启进程。`client_timeout()` 复用同一份
模块级只读配置，构造 `aiohttp.ClientTimeout`，无磁盘 I/O。
`RETRY = _retry_profile()` 与 `LIFETIME = _lifetime_profile()` 同样在
`get_token.py` import 时各读一次并归一化，`backoff_seconds()` /
`effective_lifetime()` 只做算术、不读盘。改 `IOP.retry.*` 或
`IOP.token_lifetime.*` 需重启在线进程。接口路径、`method`、`grant_type`、
请求头等契约常量不读配置，集中在 `IOP/endpoints.py`（见「请求契约集中」小节）。

### 异步与超时

原先 `IOP/get_token.py::token2global()` 虽是 `async def`，内部却用同步
`requests.post`：一旦触发 token 刷新，**整个事件循环被卡住**，所有并发请求一起等。
现改为 `aiohttp` 异步请求，两个 HTTP 端点（token / 标签查询）都配 `ClientTimeout`：

| 位置 | 改动 |
|---|---|
| `get_token.py` | `requests.post` → `aiohttp` 异步请求；服务链路不再混用 `requests` / `aiohttp` |
| `IOP/http_session.py` | 统一构造 `ClientSession(timeout=client_timeout(), skip_auto_headers=...)`，connect / sock_read / total 三层超时；两处调用点只取 `session_context()`（见「共享 ClientSession」） |
| 两处调用点 | 超时与网络异常（`aiohttp.ClientError` / `asyncio.TimeoutError`）就地捕获、记日志、降级；token 侧还清空 `access_token`，让既有重试继续 |

配置在 `config.yaml` 的 `IOP.timeout`（秒），两个端点共用一组：

| 键 | 默认 | 说明 |
|---|---|---|
| `total` | `3` | 单次请求总超时 |
| `connect` | `1` | 建连超时 |
| `sock_read` | `2` | 读响应超时 |

`client_timeout()` 对缺键用同名默认值兜底，部署沿用旧 `config.yaml` 也不会启动失败。
`total` 调大放宽慢上游、调小更快降级；**注意与重试的乘积**：`ensure_token()` 单轮
最多发 `IOP.retry.max_attempts` 次 token 请求（默认 4），失败之间还有指数退避，
所以 token 端点持续无响应时，一轮刷新的耗时约为
`max_attempts × min(total, sock_read) + Σ退避`：默认 `4 × 2 + (0.2+0.4+0.8)
= 9.4` 秒，上界 `4 × 3 + 1.4 = 13.4` 秒。要压低这个窗口，调小
`max_attempts` / `sock_read` / `total` / 退避参数（见「重试、退避与失败冷却」）。
**token 单飞 + 失败冷却后这笔代价由同一批并发请求共同承担一次，冷却期内的新批次
直接降级、完全不等待**，不再每个请求或每个新批次各付一轮。

共享 `ClientSession`、token 单飞、重试/退避/冷却、
响应校验、有效期换算、`deadline` 恒为 datetime 均已实现，
分见后续六个小节。`IOPClient` 类已明确不做（见文末「未实现」）。
`test_local/test_iop.py` 是单次画像查询脚本：命令行给电话号，复用生产模块
真实查询 IOP 并打印客户标签画像，见「单次画像查询脚本」小节。

### 共享 ClientSession

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
行为与改动前一致、不泄漏连接，但**失去连接复用**——生产必须确保走 lifespan。
该降级告警是 `WARNING`，而 `src/log_util.py` 只落盘 INFO / ERROR（见「重试、退避与失败冷却」），
所以判断部署是否漏了入口应直接看 `get_session()` 或 startup 的
「IOP 共享 ClientSession 已创建」INFO 日志，不要依赖这条告警。

**一个 session 服务两个端点**：token 用 `data=`、标签查询用 `json=` + 显式
headers，共用同一份 `skip_auto_headers=("Content-Type",)`。该参数只跳过
**自动生成**的头：token 请求不带 Content-Type（等价 `requests`），标签查询
显式传入的 `charset=gbk` / `Accept-Encoding` 不受影响、原样发出。
实测 5 次请求共享 session 的连接池只有 1 条活跃连接，改动前每次新建 session
对应 5 条 TCP 连接。会话侧本身没有专属配置项，复用 `IOP.timeout`；`config.yaml`
另有 `IOP.retry` 段供重试策略使用（见「重试、退避与失败冷却」）。

### Token 单飞

原先 `get_custinfo.py` 每次请求各自判定 `TOKENS` 是否过期、各自调
`token2global()` 刷新：N 个请求同时判定过期 → **N 份 token 请求并发打认证服务**，
彼此覆盖 `access_token` / `deadline`（惊群）。叠加当时单轮 4 次重试后，上游持续
失败时最坏是 `4 × N` 次请求、日志刷屏、延迟互相叠加（次数现由
`IOP.retry.max_attempts` 配置，见「重试、退避与失败冷却」）。

现在把「确保 token 有效」收敛成 `IOP/get_token.py::ensure_token()` 一个入口，
`get_custinfo.py` 只调它一次：

| 步骤 | 动作 |
|---|---|
| 1 快速路径 | `token_valid()` 为真直接返回，**连锁都不碰**（token 有效期内零开销） |
| 2 记代次 | 等锁前记下 `_REFRESH_GEN` |
| 3 拿锁 | `async with _get_refresh_lock()` |
| 4 二次检查 | `token_valid()` 为真（别人刷成功）**或** `_REFRESH_GEN` 已变（本批并发已有人刷过一轮）**或**已进入冷却期 → 直接返回，复用结果 |
| 5 锁内刷新 | 只有第一个请求执行一轮重试刷新（`max_attempts` 次请求、成功即返回、失败间指数退避），`finally` 里 `_REFRESH_GEN += 1` |
| 6 降级 | 仍无有效 token 时由 `get_custinfo.py` 统一返回 `{}` |

冷却期在**拿锁之前**就短路，冷却期内的请求连锁都不碰（步骤 1 之前）。

`token2global()` 保留为**单次刷新原语**，只在单飞锁内被调用；业务侧不要直接
调它，否则绕开保护。`token_valid()` 是
`access_token is not None and now <= deadline`；前者是「拿没拿到 token」的
业务条件，`deadline` 恒为 datetime，这一半不再兼作类型兜底
（见「Token 状态不变量」小节）。

**代次为什么必要**：只做「锁 + 二次检查」时，刷新**失败**后 token 仍无效，
每个等待者拿锁后都会再刷一轮 `max_attempts` 次——把「并发打爆上游」变成
「串行 N × max_attempts」，延迟更糟。代次让失败结果也被同一批并发共享：
等待者看到代次变化就直接返回，全部一起降级 `{}`。默认
`max_attempts=4` 时，10 并发下 `token2global()` 真实调用次数：

| 场景 | 改动前 | 改动后 |
|---|---|---|
| 刷新成功 | 10（每请求 1 次） | **1** |
| 刷新失败 | 40（每请求 4 次） | **4** |

**跨 event loop**：`asyncio.Lock` 一旦发生争用就绑定创建它的 loop，跨 loop 会抛
`RuntimeError`（与共享 ClientSession 同一个坑）。故 `_get_refresh_lock()` 按当前
`asyncio.get_running_loop()` 懒创建/重建，不在模块级放裸锁；生产单 loop 下始终
是同一把锁，TestClient / 独立脚本多次 `asyncio.run()` 也不会炸。不需要 lifespan
接线，不改 `main.py`。

### 重试、退避与失败冷却

原先 `ensure_token()` 里是「循环外 1 次 + `for _ in range(3)`」：想写「重试
3 次」实际是 **4 次**连续打认证服务，**无退避**、重试之间**无日志**，只有
`token2global()` 内每条请求各记一条 ERROR。token 单飞只解决「同一批并发重复刷新」，
不解决「持续失败下不同批次各自刷新」——这一节补上。

配置在 `config.yaml` 的 `IOP.retry`，import 时由 `_retry_profile()` 读一次
（请求路径 0 次读盘）。缺段、类型错误、NaN / inf / 越界一律回落默认，
部署沿用旧 `config.yaml` 不会启动失败。

| 键 | 默认 | 说明 |
|---|---|---|
| `max_attempts` | `4` | 单轮刷新**请求总次数**（不是「重试次数」）；最小 1、上限 20 |
| `backoff_base` | `0.2` | 第 1 次失败后的退避秒数 |
| `backoff_factor` | `2` | 退避倍率，最小 1 |
| `backoff_max` | `2` | 单次退避上限，`min(base × factor^(k-1), max)` |
| `cooldown` | `5` | 一轮**全失败**后的冷却秒数，0 表示不冷却 |

默认退避序列（4 次尝试之间有 3 次）：`0.2 / 0.4 / 0.8` 秒。

**单轮流程**（全部在单飞锁内，只有持锁者执行）：

1. INFO `IOP token 刷新开始 | max_attempts=N`
2. 逐次 `await token2global()`；`token_valid()` 为真即记
   `IOP token 刷新成功 | attempt=k/N | cost=...ms`、清冷却、返回
3. 未成功且还有下一次：ERROR
   `IOP token 刷新重试 | attempt=k/N | backoff=0.20s`，然后
   `await asyncio.sleep(delay)`
4. N 次全失败：ERROR
   `IOP token 刷新失败 | attempts=N | cooldown=5.0s | cost=...ms`，
   设置 `_COOLDOWN_UNTIL = time.monotonic() + cooldown`
5. `finally` 里 `_REFRESH_GEN += 1`，与单飞代次共享

`token2global()` 仍是单次刷新原语，超时收口与
`IOP token 请求失败` 日志**前缀沿用**（响应校验给它补了 `status=` 与具体原因，
见下一小节；有效期换算只改成功分支）。本节新增的是「第几次 / 退避多久 /
本轮汇总 / 冷却」这四类信息。

**日志级别**：`src/log_util.py` 只落盘 `INFO` / `ERROR` 两个级别，
`WARNING` 既没有文件 sink 也没有控制台 sink，**写了也看不到**。因此重试用
ERROR（与 `token2global` 的失败日志同文件、语义也确实是一次失败尝试），
冷却提示用 INFO。后续若想改回 WARNING，必须同时给 `log_util.py` 加 sink。

**失败冷却**：截止点用 `time.monotonic()`，不受系统时间回拨影响。冷却期内
`ensure_token()` 在**拿锁之前**直接短路，连锁都不碰：所有请求立即
降级 `{}`，**不再打认证服务、不再等 4 次超时**。一个冷却周期只打一条
`IOP token 处于失败冷却期 | remain=...s` INFO（`_COOLDOWN_WARNED_UNTIL`
按截止点去重），避免每个失败请求刷屏；「IOP token 不可用」仍是每请求
一条。冷却到期后的第一个请求开启新一轮刷新；刷新成功会立即把
`_COOLDOWN_UNTIL` 清 0，不会误伤后续正常请求。

**与单飞的关系**：代次让**同一批并发**共享一轮（默认 10 并发失败 40 → 4 次），
冷却让**不同批次**共享失败结果（冷却期内 N 个新批次 → 0 次）。二次检查里也
判断了冷却期，防止某请求在等锁期间刚好由另一批触发了新的冷却。

**耗时**：单轮最坏约 `max_attempts × min(total, sock_read) + Σ退避`，默认
`4 × 2 + 1.4 = 9.4` 秒、上界 `4 × 3 + 1.4 = 13.4` 秒（比改动前多出 1.4 秒
退避，换来认证服务不被反复打）。但**冷却期内的请求为 0 等待**，持续故障
场景下整体延迟反而大幅下降。要缩短单轮窗口，调小 `max_attempts` /
退避参数 / `sock_read` / `total`。

`config.yaml` 只新增 `IOP.retry` 一段；`IOP.timeout`、URL、凭据、请求头等均未动。

### Token 响应校验

改动前 `token2global()` 只判 `'access_token' in res_data` / `elif 'error' in res_data`
两种结构，**完全没读 `response.status`**，三条隐患叠在一起：

| 上游响应 | 改动前行为 |
|---|---|
| 502 / 网关错误页（body 是合法 JSON，如 `{"res_code":"1"}`） | 既无 `access_token` 也无 `error`，**什么都不做**，静默保留旧 token |
| HTML 错误页 / 空 body | `response.json()` 抛异常，被外层 `except ValueError` 兜住，但日志只有笼统的 `error=`，无 status、无原因 |
| `res_data` 为 `None` / list | `'access_token' in None` 抛 `TypeError` 穿透到 `main.py` 的 `except`，IOP 侧无日志 |
| 上游返回 `error` | `my_log.error(str(res_data))` 打整个 dict，无 status、非结构化 |

最危险的是第一条：旧 `access_token` 可能已被吊销或即将过期，刷新失败却继续沿用，
且重试循环因为 `token_valid()` 为真会提前 break，把失败伪装成成功。

现在所有失败分支统一走 `_fail_token(status_code, reason)`：

1. **先查状态码**：`status_code = response.status`，非 200 直接失败、不读 body。
   502 / 网关页即使 body 是合法 JSON 也不再进入「按字段判断」的路径。
2. **解析就地收口**：`response.json(content_type=None)` 的
   `aiohttp.ClientError` / `ValueError` 内层捕获，带 status 与异常类型。
3. **结构校验**：`res_data` 非 dict（list / 字符串 / None）直接失败。
4. **字段校验**：`access_token` 为空值也判失败；上游 `error` 改成结构化
   `error=` / `error_description=`；**既无 `access_token` 也无 `error` 明确报
   「响应缺少 access_token 且无 error 字段」**。
5. **统一清空**：`_fail_token()` 把 `access_token` 置 `None`、`deadline` 置回
   `_INVALID_DEADLINE`（过去时间）并记 ERROR。此前这里写的是 `deadline = None`，
   靠 `token_valid()` 的短路顺序躲开与 `None` 比较；现在两个字段都是明确的
   「无有效 token」状态，不残留过期的 deadline，也不依赖短路顺序。
6. **网络异常**：`ClientError` / `TimeoutError` 收口保留，改调
   `_fail_token()`，日志带 `status=None`（建连阶段还没拿到响应）。

日志前缀仍是 `IOP token 请求失败`，只新增 `status=` 与具体原因；项目只落盘
INFO / ERROR，故用 ERROR（见上文「日志级别」）。

**当时的边界已由有效期换算补齐**：响应校验落地时只判响应成败，没有碰成功分支里
`int(expires_in) - 360` 那一行——上游返回合法 `access_token` 但 `expires_in`
是非法字符串时会抛 `ValueError` 穿透；`expires_in <= 360` 时 deadline 落在
过去、每个请求都判过期。两者现在都由 `effective_lifetime()` 处理，
见下一小节。响应成败与有效期换算是两类不同的问题，排查时别混为一谈。

`token2global()` 仍是单次刷新原语，只在单飞锁内被调用；响应校验只改
成败判断，有效期换算只改成功分支，二者都不改请求形状、重试次数、
退避与冷却。

### Token 有效期

改动前成功分支只有两行：

```python
expires_in = res_data.get('expires_in', 600)
TOKENS.deadline = datetime.datetime.now() + datetime.timedelta(seconds=(int(expires_in) - 360))
```

两个隐患：

| 上游返回值 | 改动前行为 |
|---|---|
| `expires_in` 缺失 / `None` / `"abc"` / `""` / `bool` / `NaN` | `int()` 抛 `TypeError` / `ValueError` 穿透到 `main.py` 的 `except`：下游降级 `{}`，**IOP 侧无日志**，重试循环的 `finally` 只保证代次 +1 |
| `expires_in = 300`（或任何 `<= 360`） | `300 - 360 = -60`，deadline 落在过去 → `token_valid()` 恒为假 → **每个请求都判过期、每次都刷新 token**，认证服务被打满 |
| `expires_in` 极大（如 `1e15`） | `timedelta` 溢出 |

现在由 `effective_lifetime(raw)` 统一换算，配置在 `config.yaml` 的
`IOP.token_lifetime`，import 时由 `_lifetime_profile()` 读一次：

| 键 | 默认 | 说明 |
|---|---|---|
| `default_expires_in` | `600` | 上游未给或 `expires_in` 非法时的兜底秒数；最小 1 |
| `refresh_buffer` | `360` | 提前刷新缓冲（秒），最小 0 |
| `min_lifetime` | `60` | 有效期下限（秒），保证 deadline 落在未来；最小 1 |
| `max_expires_in` | `86400` | 上游声明值上限（秒），防 `datetime` 溢出；最小 1 |

缺段、类型错误、`NaN` / `inf` / 负数 / 越界一律回落默认，部署沿用旧
`config.yaml` 不会启动失败。实际有效期公式：

```
v   = clamp(上游 expires_in, (0, max_expires_in])      # 非法/过大时兜底或截断
ttl = min(max(v - refresh_buffer, min_lifetime), v)    # 夹在 [下限, 上游声明值]
```

`min(..., v)` 这一层保证**永不超过上游自己声明的有效期**：上游只给 30 秒时
不会硬撑到 `min_lifetime=60` 秒。默认值下 `600 → 240` 秒，与改动前完全一致。

| 上游 `expires_in` | 实际有效期 | 说明 |
|---|---|---|
| 缺失 | 240s | 静默用 `default_expires_in`，不记日志（上游可能本来就不给该字段） |
| `600` / `"600"` | 240s | 与改动前一致；数字字符串现在也能接受 |
| `360` | 60s | 不再出现 0 或负有效期 |
| `300` | 60s | 不再落在过去、不再每请求刷新 |
| `30` | **30s** | 不超过上游声明值 |
| `"abc"` / `None` / `""` / `true` / `NaN` / `0` / `-1` | 240s | 回落默认值 |
| `1e15` | 86040s | 截断到 `max_expires_in=86400` 后按 `86400 - 360` 计算，不溢出 |

字段**存在但异常**时记一条 `INFO`：

```
IOP token expires_in 异常 | 非数字('abc') | 按 240s 计算有效期
```

用 `INFO` 而非 `ERROR`：token 本身有效、服务正常，只是上游字段不规范，
不该算故障；但项目只落盘 INFO / ERROR，所以用 INFO 保证留痕（见「重试、退避与失败冷却」的
「日志级别」）。日志不含 `access_token` 的值。

`token2global()` 仍先 `TOKENS.deadline = ...` 再写 `access_token`，顺序不变。
deadline 仍用 `datetime.datetime.now()`（本地墙上时间），**系统时间回拨**
不在本节范围内——冷却截止点已用 `time.monotonic()`，两者互不影响。
「无有效 token」时的 deadline 统一为过去时间，见下一小节。

### Token 状态不变量

改动前 `Tokens.deadline` 初始为 `None`，`token_valid()` 写成：

```python
return (TOKENS.access_token is not None
        and datetime.datetime.now() <= TOKENS.deadline)
```

`now <= None` 会抛 `TypeError`，全靠 `access_token is not None` **排在第一位**
短路才不炸。这是一个隐式依赖：一旦有人调整判断顺序、或某个失败分支只把
`access_token` 置 `None` 而忘了 `deadline`，就会立刻出问题；
`_fail_token()` 也确实把 `deadline` 写成 `None`，等于每次失败后又把状态退回
需要短路保护的样子。

现在引入模块级常量，把「无有效 token」表达成一个确定的过去时间点：

```python
_INVALID_DEADLINE = datetime.datetime.min
```

| 位置 | 改动前 | 改动后 |
|---|---|---|
| `Tokens.__init__()` | `deadline = None` | `deadline = _INVALID_DEADLINE` |
| `_fail_token()`（所有失败分支） | `deadline = None` | `deadline = _INVALID_DEADLINE` |
| `token_valid()` docstring | 注明「短路顺序保护 deadline 初始为 None」 | 改为说明 `deadline` 恒为 datetime、`access_token` 检查是业务条件 |

于是 `TOKENS.deadline` 在**所有路径**下都是 `datetime` 对象：初始化、刷新成功、
响应校验的各类失败、异常有效期取值、失败冷却。`token_valid()` 里的 `and` 仍保留，
但含义从「兼作类型兜底」收敛为纯粹的业务判断——「没拿到 token 就不算有效」，
即便某个 deadline 恰好落在未来也是如此。

`test_local/test_iop.py` 现为**单次画像查询脚本**（见「单次画像查询脚本」小节），
只调 `get_custom_info_in_global()`，不持有 `Tokens`、不给 `TOKENS.deadline`
赋值，也没有场景间的状态复位，因此状态不变量不需要在这个脚本里加任何代码；
不变量只由生产模块 `IOP/get_token.py` 一处维护。

改动量只有几行，且**不改变任何对外行为**：`datetime.min` 恒在过去，
`token_valid()` 在初始态与失败态仍为 `False`；单飞快速路径、失败冷却、
`{}` 降级、有效期计算全部不受影响。

自检：新建 `Tokens()` 后 `isinstance(deadline, datetime)` 为真、
`deadline < now`、`token_valid()` 为 `False`；把判断顺序反写成
`now <= deadline and access_token is not None` 也不抛异常（此前必抛
`TypeError`）；跑完各类失败分支后 `deadline` 仍是 datetime，不是 None。

### 上游既定约定（不可改动）

契约值集中在 `IOP/endpoints.py`，两个调用点也保留锁定注释，后续优化不要
"顺手修好"：

- `access_token` 必须放在 URL 查询串，上游不接受 `Authorization` 头。
- 两个端点只提供 `http` 明文；`client_id` / `client_secret` 以表单串提交，
  凭据存放于 `config.yaml`。
- 请求头必须声明 `Content-Type: application/json; charset=gbk` 并显式携带
  `Accept-Encoding: gzip, deflate`，即使与实际序列化编码不一致也照发。

换成 `aiohttp`、收敛为共享 `ClientSession`、再加重试/退避/冷却、
响应校验与有效期换算后，上述各项逐字保留——重试只改变「发几次、间隔多久」，
响应校验只改变「怎么判断成败」，有效期换算只改变「拿到 token 后 deadline 怎么算」，
都不改变单次请求的形状。额外一处
等价处理：`requests` 对字符串 `data=` 不加 `Content-Type`，而 `aiohttp` 默认会补
`text/plain; charset=utf-8`，所以共享 session 统一带
`skip_auto_headers=("Content-Type",)`。它只跳过自动生成的头，标签查询显式传入
的 `charset=gbk` / `Accept-Encoding` 仍原样发出，实测两种请求的上游请求头与
改动前逐一一致。这不是新约定，是把库切换与会话共用带来的副作用消掉。

### 请求契约集中（`IOP/endpoints.py`）

两个端点的 URL 路径、查询参数名、OAuth2 固定参数与请求头集中在
`IOP/endpoints.py`；`get_token.py` / `get_custinfo.py` 只调用
`token_request(cfg)` / `custinfo_request(cfg, tel, access_token)`，不再各自拼接
字符串。仍由 `config.yaml` 提供的是部署与业务参数：

| 位置 | 内容 |
|---|---|
| `IOP/endpoints.py` | `http` scheme、两个端点路径、`qryCustInfoByChannel`、`format=json`、`grant_type=client_credentials`、`charset=gbk` / `Accept-Encoding` 请求头 |
| `IOP.TOKEN` | host / port / id / secret |
| `IOP.CustInfo` | host / port / version / type |
| `IOP.timeout` / `retry` / `token_lifetime` | 超时、重试退避冷却、有效期换算 |

`token_request()` 用 `urllib.parse.urlencode` 构造表单串：参数顺序与原先一致，
对当前凭据（纯字母数字）输出逐字相同；凭据含 `&` / `=` 等字符时不再把参数
拆坏。这是「直接拼字符串」改成库编码带来的唯一差异，不改变 http 明文、
token 放查询串、凭据存放位置或请求头；`version` / `type` 仍从配置读取。
`custinfo_request()` 每次返回一份新的 headers dict，避免调用方改到常量。

改动任一契约值只需改 `endpoints.py` 一处；新增 IOP 接口时在文件里加一个
`*_request()` 即可，调用点不需要知道路径与参数名。对同一份 `config.yaml`，
两个 `*_request()` 的输出与原先在两个调用点里逐个拼接的结果逐字一致，
可用自检要点里的比对方法复核。

### 单次画像查询脚本（`test_local/test_iop.py`）

用途回到最初的调试脚本：**跑一次、给一个电话号、把查到的客户画像打印出来**。
明确不做自动化单元测试——没有断言、假响应、场景矩阵，也不打桩；它会向
`config.yaml` 里的 IOP 地址发起**真实请求**。

运行（项目根目录）：

```bash
python -m test_local.test_iop 13900000000
python test_local/test_iop.py 13900000000
```

电话号是命令行位置参数；也可以把号码填进脚本顶部的 `DEFAULT_TEL`，之后不带
参数直接运行。不带参数且 `DEFAULT_TEL` 为空时只打印用法并以退出码 2 结束，
**不发任何请求**。代码里不写死任何真实号码。

与最初的复制版相比，只保留一个改进：**不复制请求实现**。脚本只调
`IOP.get_custinfo.get_custom_info_in_global()`，URL / headers / post_data /
Tokens / 有效期换算全部走生产模块，因此 token 单飞、重试退避冷却、
有效期换算、响应校验、超时收口、会话生命周期都自动带上，生产侧改了不会漏。

| 项 | 做法 |
|---|---|
| 单次运行 | `asyncio.run(_query(tel))` 一次，查完打印就退出 |
| 会话 | 独立脚本不进 uvicorn 的 lifespan，显式 `await init_session()` / `finally: await close_session()`，避免走「临时 session 降级」告警，且同 loop 内关闭 |
| import 时机 | 生产模块在 `_query()` 内 import；`import test_local.test_iop` 零副作用、不发请求 |
| 输出 | 逐项打印 `ITEMCODE / 名称 / 值`，再附原始 dict 的 JSON；只打印到终端，不写日志文件 |
| 空结果 | 打印「未查到标签画像」并提示看 `log/ERROR_*.log`（token 不可用 / 上游非 200 / 超时都会降级为 `{}`），退出码仍为 0 |

这里的「客户画像」指 IOP 返回的**客户标签画像** `{ITEMCODE: [ITEMNAME, ITEMVALUE]}`，
也就是在线业务 `custom_label_info` 的原始输入。业务侧的 `auxiliary_portrait`
（辅助画像）还要按产品 ID 选定的标签子集拼装，只凭电话号查不到，不属本脚本
职责。

两点注意：`config.yaml` 里的 IOP 地址是**生产地址**，运行即真实联网；生产模块
import 时 `src/log_util.py` 会按既有行为创建 `log/` 目录与当日日志，但日志内容
不含手机号，脚本自身也不写日志行。

## 业务库读取（`services/data_query.py`）

两个任务原先各有一个 `data_query.py`，逻辑重复、改字段要同步两处。现在合并为
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
`loss_control`），导入时校验列与前缀的合法性，漂移即抛 `ValueError`。

### `mode` 的三种库表形式

`mode` 按「库数量 × 表数量」划分，枚举写在 `tasks/config.py` 的 `QUERY_MODES`。
**多库多表场景不存在，不在支持范围内**，配置里出现任何其它值会在导入时抛 `ValueError`。

| mode | 库表形式 | 库文件 | 表名 | 专用字段 |
|---|---|---|---|---|
| `multi_db_single_table` | 多库单表 | 按 `product_id` 选 `{库名}.db` | 每库一张 `data_{库名}` | `db_map` |
| `single_db_multi_table` | 单库多表 | `{FILE_PREFIXES[0]}.db` | `{TABLE_NAME_PREFIX}_{crc32 % NUM_TABLES}` | 无（用 `.env` 的分表配置） |
| `single_db_single_table` | 单库单表 | `{FILE_PREFIXES[0]}.db` | `table_name` 指定的固定表 | `table_name` |

当前两个任务只用到前两种；`single_db_single_table` 已实现但暂无任务使用，
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

### 两个任务的解析结果

| 任务名（= `TASKS` key） | mode | 库文件 | 表名 | 条件与排序 | 返回列 |
|---|---|---|---|---|---|
| `opportunity_recall` | `multi_db_single_table` | `tasks/sqlite_db/opportunity_recall/{库名}.db` | `data_{库名}`，库名来自 `db_map` | `product_no = telnum` + `ORDER BY op_time DESC` | 12 列（op_time…city_id） |
| `loss_control` | `single_db_multi_table` | `tasks/sqlite_db/loss_control/F09591.db` | `data_F09591_{crc32 % NUM_TABLES}` | `product_no = telnum` + `ORDER BY op_time DESC` | 5 列（op_time, user_id, product_no, city_id, strategy_name） |

### 对外接口

| 接口 | 说明 |
|---|---|
| `query(task_name, telnum, product_id=None)` | 通用派发；`task_name` 必须是 `QUERY_SPECS` 的 key |
| `get_opportunity_recall_latest(telnum, product_id)` | 上表两行的薄封装，保持 `main.py` 调用点语义清晰 |
| `get_loss_control_latest(telnum)` | 同上 |
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

## 业务库话术重组（2 个 spec 子类）

| 子类 | 读取字段 | 重组规则 |
|---|---|---|
| `rewrite_by_spec_f09554` | `content_abstract` / `type_xl` / `cust_demand` / `rec_strategy` / `reject_reason` / `reason_note` | 拼【通话总结】【用户需求】【已推荐政策】【拒绝原因】四段，标题加粗、部分蓝色，缺值写「无」；末尾拼 `auxiliary_portrait`，换行统一替换为 `<br>` |
| `rewrite_by_spec_f09591` | `strategy_name` | 自定义标签 `<$B>` / `<$I>` / `<$U>` / `<#RRGGBB>` / `<$B$I#RRGGBB>` → `<b>` / `<i>` / `<u>` / `<span style="color: #RRGGBB">`，`</>` 按栈逆序闭合；空内容返回「无历史话术记录」 |

这些格式化都在业务侧执行，库里保存的是原始文本。

## 读取侧（`dim_reader`）

三个加载函数，全部 `mode=ro` 打开、用完即关，**在线业务绝不创建/写入维表库**。

| 函数 | 返回形状 | 在线消费方 |
|---|---|---|
| `load_lookup("label_name_mapping")` | `{label_id: display_name}` | 无（辅助画像不再做名称映射，能力保留供新线复用） |
| `load_product_selling_point()` | `{business_id: (product_name, selling_point, label_id)}` | `main.py` 循环外读入 1 次并显式传参，供 `ProductInfoRaw` 取产品名与非商机 ID 的标签子集 |
| `load_cut_in_script()` | `{business_id: {标签ID / 兜底话术: [切入, 挽留] 或 ""}}` | 无（维表业务已下线，定义保留供新线复用） |

`load_cut_in_script()` 用 `ORDER BY rowid` 取
`{key, fallback, groups} + tag_*` 原始列，把每行转成列名映射后交给
`dim_transform.build_cut_in_script_dict()`；列名、标签列顺序、兜底键全部取自
`registry.json`，不硬编码。该函数当前无在线调用方，留在 `dim_reader` 里不产生
运行时开销。

### 降级矩阵

| 场景 | 行为 |
|---|---|
| 库文件不存在 | 返回 `{}` |
| 表不存在 / 列缺失（如缺 `tag_*`） | 返回 `{}`，提示删库重建 |
| 未注册表 / 未声明 `lookup` / `cut_in_script` | 返回 `{}` |
| 查询异常、`registry.json` 缺失或损坏 | 返回 `{}` |
| 注册 xlsx 缺失 | 写入侧告警跳过，库保留上次成功版本 |

返回 `{}` 时业务行为：`ProductInfoRaw` 的产品名为 `None`、其余 ID 的
`queried_label_ids` 为 `[]`；商机回捞的标签子集是代码内固定值，辅助画像由
IOP 标签拼装、不受维表影响；控损与诊断兜底不计算画像；兜底统一返回
`code: "2"` 空话术。
**维表侧出任何问题都不会让 API 500**。同一类告警每进程只打印一次（`_WARNED` 集合），避免刷屏。

### 读取次数

不做进程级缓存，每次调用读一次全表，天然读到 `dim_watcher` 写入的最新值，
无缓存失效或脏读问题。一次请求内：

- `main.py::marketAssistant` 在循环**外**读 1 次卖点表，把同一份 dict
  显式传给每个产品对象；三个构造点都接收该参数，**每请求恰好 1 次**；
- `label_name_mapping` 与 `cut_in_script` 在线侧均不再加载。

卖点表 1361 行，本地 SQLite 开销可忽略。
后续若要省连接，可基于**各表独立的 version** 加缓存。

## 维表读取侧依赖的 registry 声明

完整配置项与校验见 `Missions_writer.md`；`dim_reader` 提供以下三类声明的读取
能力，当前在线侧实际只用到 `lookup.values`：

| 声明 | 形状 | 用途 |
|---|---|---|
| `lookup.value` | 单列 → `{key: 单值}` | `label_name_mapping` |
| `lookup.values` | 多列 → `{key: (值1, 值2, 值3)}` | `product_selling_point_match`，`values` 顺序即元组下标 |
| `cut_in_script` | `key` / `fallback` / `groups` / `tags` / `fallback_key` | 声明加工所需的原始列角色，`tags` 可写 `"dynamic_columns"` 直接引用展开结果 |

⚠️ `cut_in_script.fallback_key` 当前值 `"兜底话术"`，由 `dim_reader` /
`dim_transform` 按声明生成兜底项；在线侧已无消费方，调整取值前先确认新线的口径。

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

调用方取 `fusion_script_list[0]` 作切入、`[1]` 作挽留；值为 `""` 时 `""[0]`
会 `IndexError`，需由调用方自行兜底。当前在线侧已无该调用方。

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

维表业务路由已下线，在线侧不再按 `business_id` 命中卖点表来选业务对象。
当前与维表有关的只剩两处：

| 位置 | 加载函数 | 消费方式 |
|---|---|---|
| `main.py::marketAssistant` | `load_product_selling_point()` | 循环外读入 1 次，作为请求级共享 dict 显式传给三个构造点，不参与路由判断 |
| `services/product_info_raw.py::__init__` | 无（由调用方传入 `selling_points`） | 存 `self.product_selling_point_match_dict`；`[0]` 取产品名（仅用于日志）；商机回捞的 `queried_label_ids` 由代码内固定集提供，其余 ID 才从 `[2].split("\n")` 取 |

`load_cut_in_script()` 与 `load_lookup("label_name_mapping")` 仍由 `dim_reader`
提供，但在线侧无调用方；维表库、`registry.json` 与 `dim_transform` 的加工能力
全部保留，供客户诊断新线复用。

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

## 降级与容错汇总

| 侧 | 触发 | 行为 |
|---|---|---|
| 维表 | 库/表/列不存在、`registry.json` 异常、查询失败 | `dim_reader` 返回 `{}`，业务按「无维表」降级 |
| 业务库 | 库文件不存在、`product_id` 未映射、未注册任务名、查询异常 | `services/data_query.py` 返回 `{}`，对应 spec 子类走空记录分支 |
| 调用量库 | `log/call_sum.db` 写入失败 | 保留 `_PENDING` 增量并告警、下轮重试；`_DAY_TOTAL` 仍准确，不影响请求 |
| 调用量库 | 启动时读库失败 | 只告警、基数按 0 恢复，服务照常启动；库恢复后下次重启从真实值继续 |
| IOP | token / 标签查询超时、连接失败 | 就地记 ERROR、token 侧清空 `access_token`、最终降级 `{}`；不阻塞其它并发请求 |
| IOP | 未经 lifespan（共享 session 未初始化） | `session_context()` 降级为本次请求临时 session，只告警一次；结果正确、不泄漏连接，但失去连接复用 |
| IOP | token 刷新失败（含超时） | `ensure_token()` 单轮最多 `IOP.retry.max_attempts` 次（默认 4）、失败间指数退避，一轮全失败进入 `cooldown` 冷却；不抛异常，同一批并发共享失败结果并一起降级 `{}`；API 不 500 |
| IOP | 处于失败冷却期 | `ensure_token()` 在拿锁前直接短路，**不打认证服务、不等超时**；所有请求立即降级 `{}`；一个冷却周期只打一条 INFO 提示 |
| IOP | token 响应非 200 / 解析失败 / 结构异常 / 缺字段 / 上游 `error` | `token2global()` 统一清空 `TOKENS` 并记带 `status=` 的 ERROR；按配置重试/退避/冷却，最终降级 `{}`；**不沿用旧 token** |
| IOP | `expires_in` 缺失 / 非法 / `<= 360` / 过大 | 缺失静默用 600；非法回落默认并记一条 INFO；有效期夹在 `[min_lifetime, 上游声明值]`，deadline 永远落在未来、不抛 `ValueError`、不每请求刷新；token 照常可用 |
| 写入侧 | 注册 xlsx 缺失 | `dim_watcher` 告警跳过，库保留上次成功版本 |

**维表侧出任何问题都不会让 API 500**：`ProductInfoRaw` 的产品名为 `None`、
商机回捞的标签子集仍取代码内固定值、其余 ID 的 `queried_label_ids` 为 `[]`，
兜底统一 `code: "2"` 空话术；商机辅助画像只由 IOP 标签决定，不受维表影响，
控损与兜底不计算画像。同一类告警每进程只打印一次（`_WARNED` 集合），避免刷屏。

不做进程级缓存，每次调用读一次全表，天然读到写入侧的最新值，
无缓存失效或脏读问题；改库后下一次请求即生效，无需重启。

## 自检要点（在线侧）

- 删库后调用 `load_product_selling_point()` / `load_cut_in_script()` 应返回 `{}`
  且不创建任何文件。
- `import main` 正常，`app.router.on_startup` 为空（无定时任务）。
- 路由只有三类：商机 3 个 ID 走 `ProductInfoBySpec9554`、控损 ID 有记录时走
  `ProductInfoBySpec9591`、其余全部落 `ProductInfoRaw` 并返回 `code: "2"`
  空话术（产品名与话术均为空）。把维表里存在的业务 ID 传进去也应是 `code: "2"`，
  不能因为 `product_name` 非空而返回 `code: "0"` + 空话术。
- 商机回捞的 `queried_label_ids` 是代码内固定集，改卖点维表的 `label_id` 列
  不影响画像；`label_name_mapping` 与 `cut_in_script` 在线侧不再被加载。
- **按业务声明 IOP / 画像依赖**：`iop_need()` 对商机 3 个 ID 返回
  `PORTRAIT`、控损返回 `NONE`、其余（含维表里存在的业务 ID）返回
  `RAW_LABELS`；判定按全部 `productId` 取并集，把控损 ID 排在商机之前也不能
  漏查。
- **IOP 次数**：纯控损请求打桩后 `get_custom_info_in_global()` 应为 **0** 次；
  纯商机 / 纯兜底 / 控损+商机 / 控损+兜底 / 三混合均为 **1** 次；同一请求内
  多个产品 ID 不会各查一次。纯控损请求即使 IOP 服务不可用也不打 ERROR 日志、
  响应照常返回。
- **画像次数**：打桩 `build_auxiliary_portrait()`，控损 / 兜底 / 维表 ID 落兜底
  应为 **0** 次；纯商机 **1** 次；3 个不同商机 **3** 次；同一商机 ID 重复 3 次
  仍 **1** 次；混合请求等于该请求里不同的商机 `product_id` 数。
- **请求内隔离**：`IopLabelProvider` 与 `portrait_cache` 都在 `marketAssistant`
  函数体内创建，模块级 / `app.state` 不应有标签或画像缓存；连续两个不同手机号
  请求，响应互不串标签值、画像缓存也不串。提供者同实例第二次 `labels()`
  不再发起查询、异常仍降级 `{}` 且不抛 500。
- **维表读取次数**：打桩 `load_product_selling_point()`，一次请求应为 **1** 次，
  与列表里产品个数无关；三个构造点都显式接收同一份 dict。
- `services/data_query.py`、两个 spec 子类、`dim_reader` 均可独立导入。
- `TASKS` / `DB_SCHEMAS` / `QUERY_SPECS` 三者 key 一致；改 `.env` 的
  `LOSS_CONTROL_NUM_TABLES` 后，`get_loss_control_latest()` 解析出的表名后缀同步变化。
- `QUERY_MODES` 恰为三种；`_resolve()` 对两个任务分别解析出
  `{库名}.db` + `data_{库名}`、`F09591.db` + `data_F09591_N`。
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
- **异步请求不再阻塞事件循环**：把 IOP 请求打桩成 50ms 延迟，10 个并发
  `token2global()` 总耗时应接近 50ms（改动前用同步 `requests.post` 会串行成 ~500ms）。
  注意 `token2global()` 现在是单次刷新原语，业务侧入口是 `ensure_token()`；
  测并发刷新次数应打桩 `token2global` 后跑 `ensure_token()`。
- **超时收口**：让 token / 标签查询抛 `asyncio.TimeoutError`，应看到
  `IOP token 请求失败`（带 `status=None`）或 `IOP 标签查询网络失败` 的
  ERROR 日志、返回 `{}`，且 API 不 500；token 侧 `access_token` 与 `deadline`
  都被清空。
  把 `config.yaml` 的 IOP host/port 临时指向一个「accept 后不回数据」的本地端口
  可实测：默认 4 次 `sock_read=2` + 3 次退避（1.4 秒）约 9.4 秒，
  日志含 4 条 `IOP token 请求失败`、3 条 `IOP token 刷新重试`、
  1 条 `IOP token 刷新失败 | attempts=4 | cooldown=5.0s`，API 仍 200；
  紧接着再发请求应被冷却短路、0 次刷新、立即返回 `{}`。
- **共享 session 生命周期**：`TestClient(app)` 作为上下文进入时
  `IOP.http_session.get_session()` 非 `None` 且 `closed=False`，退出后为 `None`
  （已 `close()`）；startup 日志含「IOP 共享 ClientSession 已创建」、
  shutdown 日志含「已关闭」，二者在同一 event loop。
- **共享 session 连接复用**：起本地 HTTP server 连打 5 次标签查询，服务端 accept 次数
  应为 1（keep-alive 复用）；每次新建 session 的旧实现为 5。
- **共享 session 降级**：不把 `TestClient` 当上下文管理器时 `get_session()` 为 `None`，
  结果仍正确、API 不 500，临时 session 用后即关闭。代码里每进程只发一条
  「降级为临时 session」WARNING，但本项目不落盘 WARNING，验证以
  `get_session()` 与结果为准。
- **单飞（成功）**：打桩 `token2global` 让首次即写入有效 token，10 个并发
  `ensure_token()` 的刷新调用次数应为 **1**（改动前 10 个请求各刷 1 次 = 10）。
  token 已有效时直接调 `ensure_token()` 应为 **0** 次刷新，不进锁。
- **单飞（失败共享）**：打桩 `token2global` 始终不写 token，10 个并发
  `get_custom_info_in_global()` 的刷新调用次数应为 **`max_attempts`**（默认 4，
  改动前 10 × 4 = 40），10 个结果全为 `{}`、API 不 500。
  「IOP token 不可用」仍是每请求一条；冷却提示（INFO）每周期一条。
- **单飞跨 loop**：连续两次 `asyncio.run()` 各跑一批带争用的 `ensure_token()`，
  不应抛 `is bound to a different event loop`（`_get_refresh_lock()` 按当前 loop
  懒创建/重建）；同一 loop 内多次调用返回的是同一把锁。
- **重试次数与退避**：把 `IOP.retry.max_attempts` 设为 2 / 6，一轮全失败时
  `token2global()` 调用次数应恰好是 2 / 6（不再是隐式的 1+3=4）；
  打桩 `asyncio.sleep` 记录参数，默认 `backoff_base=0.2 / factor=2 / max=2`
  的序列应为 `0.2 / 0.4 / 0.8`；每次重试有 `IOP token 刷新重试` 的 ERROR
  （`src/log_util.py` 不落盘 WARNING）、全失败有
  `attempts=N | cooldown=...` 的 ERROR、成功有 `attempt=k/N` 的 INFO。
- **失败冷却**：一轮失败后立刻再发 10 个并发请求，`token2global()` 调用应为
  **0** 次、全部立即返回 `{}`、API 不 500，冷却 INFO 提示只打 1 条；
  把 `cooldown` 设 0.2 秒，sleep 过后再发一批应重新开启一轮刷新；
  刷新成功后 `_COOLDOWN_UNTIL` 清 0，后续请求走快速路径。
- **重试配置容错**：删掉 `IOP.retry` 整段应回落到
  `4 / 0.2 / 2 / 2 / 5` 且不抛异常；`max_attempts: 0`、负数、
  字符串、NaN、`factor < 1` 等非法值同样回落；`max_attempts > 20` 截到 20。
- **响应校验失败分支全覆盖**：给 `token2global()` 打桩依次返回 7 类响应——
  `status=502` + 合法 JSON、HTML 错误页（`json()` 抛 `ValueError`）、
  `res_data` 为 list / `None` / 字符串、`{"access_token": ""}`、
  `{"error": "invalid_client"}`、`{"res_code": "1"}`（既无 token 也无 error）——
  每次都应返回 `None`、`TOKENS.access_token is None`、
  `deadline == _INVALID_DEADLINE`（此前是 `None`）、
  记一条带 `status=` 与具体原因的 ERROR，且不抛异常。
- **响应校验不再保留旧值**：先写入一个有效 token，再让它收到
  `200 + {"res_code": "1"}`；改动前旧 `access_token` 仍在且 `token_valid()`
  为真（失败被伪装成成功），改动后应立刻变为 `False`。
- **响应校验成功路径**：`200 + {"access_token": "tk", "expires_in": 600}` →
  `token_valid()` 为真、deadline 约为 240 秒后、无 ERROR 日志；
  日志里不出现 `access_token` 的值。
- **响应校验端到端**：本地 IOP server 的 token 端点固定返回 502，起 uvicorn 打
  6 个并发业务请求；应全部 200，ERROR 日志含
  `status=502 | HTTP 状态码非 200`，且冷却期内再发一批不再打认证服务。
- **有效期对照**：直接调 `token2global()`，依次给 `expires_in` 为
  缺失 / `600` / `"600"` / `360` / `300` / `30` / `"abc"` / `None` / `""` /
  `true` / `NaN` / `0` / `-1` / `1e15`，应分别得到约
  `240 / 240 / 240 / 60 / 60 / 30 / 240 / 240 / 240 / 240 / 240 / 240 /
  240 / 86040` 秒的 deadline 偏移，且**全部** `token_valid()` 为真、不抛任何异常。
  改动前 `"abc"` / `None` / `""` 会抛 `ValueError` / `TypeError`，
  `360` / `300` / `30` 会让 deadline 落在过去。
- **有效期不再每请求刷新**：上游固定返回 `expires_in=300`，首轮刷新成功后
  连续调 10 次 `ensure_token()`，`token2global()` 调用次数应为 **0**
  （快速路径命中）；改动前每次都会重新判定过期并刷新。
- **有效期配置容错与日志**：删掉 `IOP.token_lifetime` 整段应回落到
  `600/360/60/86400`；非法值同样回落。`expires_in="abc"` 时
  `TOKENS.access_token` 照常写入、日志出现一条
  `IOP token expires_in 异常 | 非数字('abc')` 的 INFO，且不含 token 值。
- **单次画像查询脚本**：带电话号跑一次应打印「客户画像查询结果」、逐项标签
  与原始 dict（可用本地假 IOP server + 副本里临时改 `config.yaml` 的 host/port
  验证，token 端点 1 次、标签端点 1 次）；不带号应只打印用法、退出码 2、
  0 次请求；上游返回空或失败时应打印「未查到标签画像」且不抛异常。
  脚本自身不打桩、不做断言，不写死真实号码，也不复制
  `requests.post` / `grant_type=client_credentials` / `qryCustInfoByChannel` /
  `int(expires_in) - 360` 这些请求实现。
- **请求契约逐字一致**：用同一份 `config.yaml` 分别跑改动前后版本，以假
  session 捕获两端点 `client.post()` 的 `url` / `data` / `json` / `headers`，
  两组参数应逐字相同；另把 `IOP.TOKEN.secret` 临时换成含 `&` / `=` 的值，
  `token_request()` 应正确转义，直接拼字符串则会把参数拆开。
- **状态不变量**：新建 `Tokens()` 后 `isinstance(deadline, datetime)` 为真、
  `deadline < now`、`token_valid()` 为 `False`；把判断顺序反写成
  `now <= deadline and access_token is not None` 也不抛异常（此前必抛
  `TypeError`）。依次跑完初始化 / 刷新成功 / 各类失败 / 异常有效期 /
  失败冷却后，`deadline` 应始终是 datetime，从不为 `None`。
  当前单次画像查询脚本不触碰 `TOKENS`，无需状态复位。

## 未实现

- **客户诊断新线（暂不实现）**：`output_script` 计划由多张维表结合 IOP 用户画像
  拼装，不再依赖每日 AVL 宽表入库。这些表的结构与获取形式尚未确定，暂不在项目中
  实现，也不建代码占位或空模块；旧业务线已分步清理完毕，本项目不再保留其
  代码、配置、文档或样例。维表业务下线后，不属于商机回捞 / 控损的产品 ID
  全部作为诊断兜底，当前统一输出 `code: "2"` 空话术，等新线实现后再切换；
  维表库、`registry.json`、`dim_reader` / `dim_transform` 的加工能力均保留供复用。
  诊断兜底声明 `RAW_LABELS`、一定触发 IOP；当前输出尚未消费标签，属已确认的
  预留，不是遗漏的浪费。
- 按业务声明 IOP / 画像依赖已落地，不再是待办：三态声明、请求级惰性标签提供者、
  画像按 `product_id` 懒执行缓存、卖点表每请求 1 次，见「请求处理与路由」
  「公共画像拼装」与「自检要点」。
- 基于 `version` 的进程级缓存、版本回滚、历史版本查询、灰度切换。
- 加工规则的完全配置化：当前 `cut_in_str2tuple` 与分组规则写在 `dim_transform.py`，
  `registry.json` 只声明列角色与列顺序。
- 状态不变量已处理：`Tokens.deadline` 恒为 datetime，无有效 token 时统一为
  `_INVALID_DEADLINE`（过去时间），见「Token 状态不变量」小节。
  IOP 侧已无待办的隐式类型依赖。
- **`IOPClient` 类：已决定不做**。其并发正确性目标已由单飞 + 代次、
  lifespan 共享 session、失败冷却覆盖，统一失败契约由
  空 dict 降级 / 日志字段 / 响应校验提供，不再单独收敛为类。类化需同时改动 `main.py`、
  `get_custinfo.py`、`get_token.py`、`http_session.py`，并重新验证
  单飞跨 event loop 与共享 session 同 loop 约束，风险收益比不划算。
- 重试已配好次数/退避/冷却，有效期换算已完成，响应校验已覆盖状态码与
  结构校验，状态不变量已消除 `deadline=None` 的隐式短路依赖；但**冷却是进程内状态**：
  多实例部署时各实例各自冷却，不做跨进程共享。
- `test_local/test_iop.py` 现为**单次画像查询脚本**：命令行指定电话号、复用
  生产模块真实查询 IOP、打印客户标签画像，见「单次画像查询脚本」小节。
  按明确要求**不做自动化单元测试**，不再列为待办；最初「复制两份请求实现」
  的问题已随复用生产模块解决。
- 请求契约已集中：URL 路径 / `method` / `grant_type` / `charset=gbk` 请求头等
  契约常量集中在 `IOP/endpoints.py`，`version` / `type` / host / port /
  超时 / 重试 / 有效期仍由 `config.yaml` 提供，见「请求契约集中」小节。
- 另：根目录 `test_online.py` 的请求体里仍写有 `18866816199`，
  属在线接口测试脚本，不在本次调试脚本范围内。
