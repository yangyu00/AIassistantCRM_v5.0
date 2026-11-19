# IOP · 用法与结构

> **文档地图**：`IOP/` 的用法说明。
> 同包另一份：[`constraints.md`](constraints.md)（约束）；全局架构与在线业务：[根 `README.md`](../README.md)。
> - [`local_excel2db/readme.md`](../local_excel2db/readme.md) · [`local_excel2db/constraints.md`](../local_excel2db/constraints.md)
> - [`sftp_avl2db/readme.md`](../sftp_avl2db/readme.md) · [`sftp_avl2db/constraints.md`](../sftp_avl2db/constraints.md)

> `IOP/` 是用户标签（画像）查询客户端，包顶层入口 `from IOP import IOPClient`。本文件讲「怎么跑、怎么配、流程是什么」；响应校验、有效期、状态不变量与降级自检见 `constraints.md`。

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

## 返回契约与降级

**所有失败分支统一返回空 dict `{}`**，不再返回 `None`。原因是 `main.py` 的
`try/except` 只包住函数调用本身，`None` 不抛异常、不会触发 `except`，会一路传到
`ProductInfoRaw::__get_queried_label_nvs` 并抛 `TypeError`（只要
`queried_label_ids` 非空），使"token 拿不到"这个最该降级的场景反而返回 500。空 dict 则让下游
按「无标签」正常产出话术。

失败分支包括：token 不可用、token 请求超时/网络异常、**token 响应非 200 /
解析失败 / 结构异常 / 缺字段 / 上游 `error`**（详见 `constraints.md` 的「Token 响应校验」），以及标签查询连接或读超时、
响应 JSON 解析失败、非 200、`res_code != 0`、响应非 dict、缺少或非列表的
`BUSIINFO`。缺 `ITEMCODE` / 非 dict 的单条记录跳过，不拖累整批。
`res_code` 按 `str(...)` 比较，兼容上游返回字符串 `"0"` 或数字 `0`。

token 侧任一失败都会清空 `TOKENS` 并记 ERROR，按配置重试/退避/冷却，
最终仍无有效 token 时统一返回 `{}`；**不会沿用可能已过期的旧 access_token**。

## 日志字段

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

## 配置读取

IOP 的运行参数全部定义在 `IOP/config.py`，以模块级常量和冻结 dataclass 提供，
不读配置文件、不读环境变量、不依赖项目里的其它模块。`IOPClient()` 默认取
`IOPConfig.from_module_defaults()`；需要换地址、凭据或策略时，用
`IOPConfig.from_mapping({...})` 显式覆盖。

`IOPConfig` 在构造时完成缺键、类型错、NaN/inf/越界的回落校验；
`AiohttpTransport.client_timeout()` 用它构造 `aiohttp.ClientTimeout`，
`backoff_seconds()` / `effective_lifetime()` 只做算术。请求路径 0 次 I/O，
改参数需重启进程。接口路径、`method`、`grant_type`、请求头等契约常量集中在
`IOP/endpoints.py`（见「请求契约集中」小节）。

## 异步与超时

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
分见后续六个小节。这些机制由 `IOPClient` 及其组件承载，见文末 `IOPClient` 条目。
`test_local/test_iop.py` 是单次画像查询脚本：命令行给电话号，复用生产模块
真实查询 IOP 并打印客户标签画像，见「单次画像查询脚本」小节。

## 共享 ClientSession

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

## Token 单飞

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
（见 `constraints.md` 的「Token 状态不变量」）。

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
接线。

## 重试、退避与失败冷却

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
见 `constraints.md` 的「Token 响应校验」；有效期换算只改成功分支）。本节新增的是「第几次 / 退避多久 /
本轮汇总 / 冷却」这四类信息。

**日志级别**：`src/log_util.py` 只落盘 `INFO` / `ERROR` 两个级别，
`WARNING` 既没有文件 sink 也没有控制台 sink，**写了也看不到**。因此重试用
ERROR（与 `token2global` 的失败日志同文件、语义也确实是一次失败尝试），
冷却提示用 INFO。后续若想改回 WARNING，必须同时给 `log_util.py` 加 sink。

## 请求契约集中（`IOP/endpoints.py`）

两个端点的 URL 路径、查询参数名、OAuth2 固定参数与请求头集中在
`IOP/endpoints.py`；`get_token.py` / `get_custinfo.py` 只调用
`token_request(cfg)` / `custinfo_request(cfg, tel, access_token)`，不再各自拼接
字符串。部署与业务参数由 `IOP/config.py` 提供；`EndpointSpec` 把 `IOPConfig`
适配成下表同形状的输入后复用 `endpoints.py` 的公开函数，契约仍是单点。
下表「位置」列是参数分组，实际来源是 `IOP/config.py`：

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

## 单次画像查询脚本（`test_local/test_iop.py`）

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
