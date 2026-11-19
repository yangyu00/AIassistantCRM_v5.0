# IOP · 约束、降级与自检

> **文档地图**：`IOP/` 的约束说明。
> 同包另一份：[`readme.md`](readme.md)（用法）；全局架构与在线业务：[根 `README.md`](../README.md)。
> - [`local_excel2db/readme.md`](../local_excel2db/readme.md) · [`local_excel2db/constraints.md`](../local_excel2db/constraints.md)
> - [`sftp_avl2db/readme.md`](../sftp_avl2db/readme.md) · [`sftp_avl2db/constraints.md`](../sftp_avl2db/constraints.md)

> 本文件是改动 `IOP/` 前必须读的规矩：上游既定约定、Token 校验与有效期、状态不变量、降级行为与自检要点。用法与配置见 `readme.md`。

## 失败冷却与耗时边界

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

## Token 响应校验

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
INFO / ERROR，故用 ERROR（见 `readme.md` 的「日志级别」）。

**当时的边界已由有效期换算补齐**：响应校验落地时只判响应成败，没有碰成功分支里
`int(expires_in) - 360` 那一行——上游返回合法 `access_token` 但 `expires_in`
是非法字符串时会抛 `ValueError` 穿透；`expires_in <= 360` 时 deadline 落在
过去、每个请求都判过期。两者现在都由 `effective_lifetime()` 处理，
见下文「Token 有效期」。响应成败与有效期换算是两类不同的问题，排查时别混为一谈。

`token2global()` 仍是单次刷新原语，只在单飞锁内被调用；响应校验只改
成败判断，有效期换算只改成功分支，二者都不改请求形状、重试次数、
退避与冷却。

## Token 有效期

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
不该算故障；但项目只落盘 INFO / ERROR，所以用 INFO 保证留痕（见 `readme.md` 的「重试、退避与失败冷却」的
「日志级别」）。日志不含 `access_token` 的值。

`token2global()` 仍先 `TOKENS.deadline = ...` 再写 `access_token`，顺序不变。
deadline 仍用 `datetime.datetime.now()`（本地墙上时间），**系统时间回拨**
不在本节范围内——冷却截止点已用 `time.monotonic()`，两者互不影响。
「无有效 token」时的 deadline 统一为过去时间，见下文「Token 状态不变量」。

## Token 状态不变量

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

`test_local/test_iop.py` 现为**单次画像查询脚本**（见 `readme.md` 的「单次画像查询脚本」小节），
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

## 上游既定约定（不可改动）

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

## 降级与容错（IOP）

| 侧 | 触发 | 行为 |
|---|---|---|
| IOP | token / 标签查询超时、连接失败 | 就地记 ERROR、token 侧清空 `access_token`、最终降级 `{}`；不阻塞其它并发请求 |
| IOP | 未经 lifespan（共享 session 未初始化） | `session_context()` 降级为本次请求临时 session，只告警一次；结果正确、不泄漏连接，但失去连接复用 |
| IOP | token 刷新失败（含超时） | `ensure_token()` 单轮最多 `IOP.retry.max_attempts` 次（默认 4）、失败间指数退避，一轮全失败进入 `cooldown` 冷却；不抛异常，同一批并发共享失败结果并一起降级 `{}`；API 不 500 |
| IOP | 处于失败冷却期 | `ensure_token()` 在拿锁前直接短路，**不打认证服务、不等超时**；所有请求立即降级 `{}`；一个冷却周期只打一条 INFO 提示 |
| IOP | token 响应非 200 / 解析失败 / 结构异常 / 缺字段 / 上游 `error` | `token2global()` 统一清空 `TOKENS` 并记带 `status=` 的 ERROR；按配置重试/退避/冷却，最终降级 `{}`；**不沿用旧 token** |
| IOP | `expires_in` 缺失 / 非法 / `<= 360` / 过大 | 缺失静默用 600；非法回落默认并记一条 INFO；有效期夹在 `[min_lifetime, 上游声明值]`，deadline 永远落在未来、不抛 `ValueError`、不每请求刷新；token 照常可用 |

## 自检要点（IOP · 请求侧）

- **IOP 次数**：纯控损请求打桩后 `get_custom_info_in_global()` 应为 **0** 次；
  纯商机 / 纯兜底 / 控损+商机 / 控损+兜底 / 三混合均为 **1** 次；同一请求内
  多个产品 ID 不会各查一次。纯控损请求即使 IOP 服务不可用也不打 ERROR 日志、
  响应照常返回。

## 自检要点（IOP · 客户端）

以下条目沿用原「自检要点（在线侧）」中的 IOP 部分，未改断言口径：

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

## 未实现 / 边界（IOP）

- 状态不变量已处理：`Tokens.deadline` 恒为 datetime，无有效 token 时统一为
  `_INVALID_DEADLINE`（过去时间），见「Token 状态不变量」小节。
  IOP 侧已无待办的隐式类型依赖。
- **`IOPClient`**：包顶层公开入口 `from IOP import IOPClient`，零参数可用：
  `async with IOPClient() as c: await c.get_customer_labels(tel)`。
  配置全部定义在 `IOP/config.py`，日志由构造时注入，未注入则完全静默。
  模块划分：`config.py` 参数、`endpoints.py` 请求契约、`transport.py` 会话、
  `token.py` 单飞与重试、`labels.py` 响应解析、`client.py` 门面。
  统一降级 `{}`、单飞、重试退避冷却、有效期换算、7 类响应校验均保持不变。
- 重试已配好次数/退避/冷却，有效期换算已完成，响应校验已覆盖状态码与
  结构校验，状态不变量已消除 `deadline=None` 的隐式短路依赖；但**冷却是进程内状态**：
  多实例部署时各实例各自冷却，不做跨进程共享。
- `test_local/test_iop.py` 现为**单次画像查询脚本**：命令行指定电话号、复用
  生产模块真实查询 IOP、打印客户标签画像，见 `readme.md` 的「单次画像查询脚本」小节。
  按明确要求**不做自动化单元测试**，不再列为待办；最初「复制两份请求实现」
  的问题已随复用生产模块解决。
- 请求契约已集中：URL 路径 / `method` / `grant_type` / `charset=gbk` 请求头等
  契约常量集中在 `IOP/endpoints.py`；`version` / `type` / host / port /
  超时 / 重试 / 有效期由 `IOP/config.py` 提供。`EndpointSpec` 复用 `endpoints.py`
  的公开函数构造请求，契约仍是单点，见 `readme.md` 的「请求契约集中」小节与上文 `IOPClient` 条目。
