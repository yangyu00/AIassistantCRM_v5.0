# IOP · 用户标签查询客户端

`IOP/` 是用户标签（画像）查询客户端，公开入口只有 `IOPClient`：

```python
from IOP import IOPClient

async with IOPClient() as client:
    labels = await client.get_customer_labels("13900000000")
# 返回 {ITEMCODE: [ITEMNAME, ITEMVALUE], ...}
```

日常只改 `IOP/.env`：地址、凭据、超时、重试、有效期都在那一个文件里，改完重启进程即可。`import IOP` 不读任何文件，配置在构造客户端时才加载并冻结；但包根 `__init__.py` 顶层导入 `transport / token / client`，三者模块顶层 `import aiohttp`，所以 **`import IOP` 需要 `aiohttp` 已安装**。

`get_customer_labels(tel)` 是主业务入口，任何失败统一降级为 `{}`，不返回 `None`（返回 `None` 不会触发上层 `except`，会在调用方按 dict 使用时抛 `TypeError`）。`get_customer_labels_with_status(tel)` 额外返回 `(labels, status)`，把「上游成功但号码无标签」与「IOP 降级」区分开；线上 `main.py` 用后者把 `status` 记进日志。成功但 0 标签的 `status` 是 `ok_empty`。

**线上必须注入 logger**：`IOPClient()` 不传 logger 时用 `NullLogger`，`info/error/warning` 全是空操作，所有刷新失败 / 冷却降级 / 标签查询失败都不会落盘。`main.py` 用 `IOPClient(logger=my_log)` 接进在线进程的文件日志。

## 1. 模块划分

依赖单向：`config` → `endpoints` → `transport` / `token` / `labels` → `client`。

| 文件 | 职责 |
|---|---|
| `config.py` | 读 `IOP/.env`、默认值与数值容错、冻结配置对象 |
| `endpoints.py` | 上游请求契约：两个端点的 URL / 参数 / 请求头，两个纯函数，按传入的 `IOPConfig` 现算 |
| `transport.py` | `aiohttp` 共享会话与响应收口，`Transport` 协议可注入替身 |
| `token.py` | token 刷新原语、单飞、重试退避冷却、响应校验、有效期 |
| `labels.py` | 标签响应的逐层校验与 `BUSIINFO` 组装（纯函数） |
| `client.py` | 业务门面，编排「确保 token → 请求 → 校验 → 降级」 |

`client.py` 与 `token.py` 都直接调用 `endpoints.py` 的纯函数。打桩 / 换实现只需注入 `Transport` 一种替身。

## 2. 请求链路

1. **启动预热**：lifespan `startup` 里 `IOPClient.start()` 尽力预热 token（`IOP_WARMUP_TIMEOUT` 秒内），失败 / 超时只记 ERROR，不阻塞服务启动。
2. **后台续期**：`start()` 起一个 `asyncio.Task`，在 `deadline - IOP_BACKGROUND_REFRESH_LEAD` 处续期，`shutdown` 时取消。刷新不再落在用户请求的关键路径上。
3. `ensure_token()`：token 有效则零开销直接返回；否则在单飞锁内完成一轮刷新。**续期刷新失败时保留仍在有效期内的旧 token**（记 ERROR），避免一次上游抖动把整个进程打回无 token 状态。
4. 刷新后仍无有效 token，返回 `{}` + `status=token_unavailable`，不发标签请求；冷却期内的请求同样在这里短路（0 次 HTTP），但会记 INFO。
5. 标签查询用 `json=` + 显式 headers 发出；建连 / 读超时与网络异常就地收口，返回 `{}` + 对应 `status`。
6. `parse_custinfo_response_ex()` 依次校验：`status == 200`、响应体为 dict、`res_code` 按字符串比较等于 `"0"`、`result.BUSIINFO` 为 list；单条缺 `ITEMCODE` 或非 dict 的记录跳过。返回 `(labels, status, auth_failed)`。
7. **token 失效自愈**：`auth_failed=True` 时（HTTP 401/403、`res_code` 命中 `IOP_AUTH_FAILURE_CODES`、或错误描述命中 `IOP_AUTH_FAILURE_KEYWORDS`），`invalidate()` 清空本地 token → `ensure_token(force=True)` 跳过冷却短路重刷一次 → 用新 token **重试一次**标签查询。`IOP_AUTH_RETRY_ENABLED=0` 可关闭。

## 3. 上游约定（不可改动）

- 两个端点只提供 `http` 明文；token 端点固定 `grant_type=client_credentials`，`client_id` / `client_secret` 以 `urlencode` 表单串提交。
- `access_token` 必须放在标签查询 URL 的查询串，上游不接受 `Authorization` 头。
- 标签请求头必须显式声明 `Content-Type: application/json; charset=gbk` 与 `Accept-Encoding: gzip, deflate`，即使与实际序列化编码不一致也照发；响应解析用 `json(content_type=None)`，不做 Content-Type 类型校验。
- token 请求用字符串 `data=`，不带 Content-Type。共享 session 统一带 `skip_auto_headers=("Content-Type",)`，只跳过自动生成的头，显式传入的 `charset=gbk` / `Accept-Encoding` 原样发出。

端点路径与固定参数（`IOP/endpoints.py` 当前值，重建时照此写）：

- token 端点：`http://{IOP_TOKEN_HOST}:{IOP_TOKEN_PORT}/openapi/restOauth2Server/access_token`，表单串字段 `grant_type=client_credentials` / `client_id` / `client_secret`。
- 标签端点：`http://{IOP_CUSTINFO_HOST}:{IOP_CUSTINFO_PORT}/openapi/httpService/UserService`，固定查询参数 `access_token` / `method=qryCustInfoByChannel` / `format=json`；`post_data` 为 `VERSION`（取 `IOP_CUSTINFO_VERSION`）/ `TELNUM` / `TYPE`（取 `IOP_CUSTINFO_TYPE`）。

契约值集中在 `endpoints.py`；新增 IOP 接口时在该文件加一个 `*_request(cfg, ...)` 即可。

## 4. 配置

唯一来源 `IOP/.env`，唯一入口 `IOPConfig.load()`。不读系统环境变量，没有路径覆盖或映射覆盖入口。改完 `.env` 重启进程生效（`IOPClient()` 构造时读一次，结果冻结）。

| 文件 | 作用 |
|---|---|
| `IOP/.env` | 与 `config.py` 同目录，日常改这个 |
| `IOP/.env.example` | 带注释的键说明模板，与 `.env` 同值；新环境可 `cp IOP/.env.example IOP/.env` |
| `IOP/config.py` 的 `_SPECS` | 28 个键的清单、默认值与数值下界；新增参数在这里加一行并写进 `.env.example` |

`.env` 语法只认整行 `#` 注释和 `KEY=VALUE`，不做引号、行内注释或 `${VAR}` 插值。确认生效值（配置对象冻结，`repr` 自动屏蔽 `secret`）：

```bash
python -c "from IOP import IOPConfig; print(IOPConfig.load())"
```

### 键表

| 分组 | `.env` 键 | 默认值 |
|---|---|---|
| token 端点 / 凭据 | `IOP_TOKEN_HOST` / `IOP_TOKEN_PORT` / `IOP_TOKEN_ID` / `IOP_TOKEN_SECRET` | `134.80.220.55` / `80` / 见 `.env` / 见 `.env` |
| 标签端点 | `IOP_CUSTINFO_HOST` / `IOP_CUSTINFO_PORT` / `IOP_CUSTINFO_VERSION` / `IOP_CUSTINFO_TYPE` | `134.80.220.55` / `80` / `1.0.1` / `M` |
| 超时（秒） | `IOP_TIMEOUT_TOTAL` / `IOP_TIMEOUT_CONNECT` / `IOP_TIMEOUT_SOCK_READ` | `3` / `1` / `2` |
| 重试 / 退避 / 冷却 | `IOP_RETRY_MAX_ATTEMPTS` / `IOP_RETRY_BACKOFF_BASE` / `IOP_RETRY_BACKOFF_FACTOR` / `IOP_RETRY_BACKOFF_MAX` / `IOP_RETRY_COOLDOWN` | `4` / `0.2` / `2` / `2` / `5` |
| token 有效期（秒） | `IOP_LIFETIME_DEFAULT_EXPIRES_IN` / `IOP_LIFETIME_REFRESH_BUFFER` / `IOP_LIFETIME_MIN_LIFETIME` / `IOP_LIFETIME_MAX_EXPIRES_IN` | `600` / `360` / `60` / `604800` |
| 有效期字段兼容 | `IOP_EXPIRES_IN_FIELDS` | `expires_in,expiresIn,expire_in` |
| token 失效自愈 | `IOP_AUTH_FAILURE_CODES` / `IOP_AUTH_FAILURE_KEYWORDS` / `IOP_AUTH_RETRY_ENABLED` | `401,403,1001,1002,10001,10102,30001` / 见 `.env` / `1` |
| 后台续期 / 预热 | `IOP_BACKGROUND_REFRESH_ENABLED` / `IOP_BACKGROUND_REFRESH_LEAD` / `IOP_BACKGROUND_REFRESH_MIN_INTERVAL` / `IOP_WARMUP_TIMEOUT` | `1` / `60` / `30` / `5` |

### 容错与配置回落

- 缺键 / 数值填错不报错：回落 `config.py` 内置默认值。非数字、NaN / inf、低于下界一律回落；`max_attempts` 硬上限 20（防止配错导致长时间串行循环），`cooldown=0` 表示不冷却，`IOP_TIMEOUT_*` 填 0 或过小值回落默认。文件不存在时同理。
- 没有凭据时直接回落：缺文件、缺凭据键、值为空或显式写成 `<...>` 一类占位符时，回落到 `config.py` 内置实际值，不把占位符当成真实配置。
- `IOPConfig.__repr__` 屏蔽 `secret`；`IOP/.env` 与 `.env.example` 随仓库入库，里面就是实际生效字符串。

## 5. 并发与可靠性约束

- **共享会话**：`AiohttpTransport` 只创建一个 `aiohttp.ClientSession`，`start()` / `aclose()` 对应应用 lifespan 的 startup / shutdown，幂等且异常不外抛；session 绑定创建它的 event loop，必须在同一 loop 内关闭。未经 lifespan 时（独立脚本、TestClient）降级为「本次请求一个临时 session」，结果正确、不泄漏连接。
- **token 单飞**：`ensure_token()` 是唯一入口；`refresh_once()` 仅作为单次刷新原语在锁内调用。`asyncio.Lock` 按当前 event loop 懒创建 / 重建；「刷新代次」让同一批并发共享一轮结果（含失败）。
- **重试、退避与冷却**：单轮最多 `max_attempts` 次请求，失败间按 `min(base × factor^(k-1), max)` 指数退避；全失败后进入 `cooldown` 冷却，截止点用 `time.monotonic()`（不受系统时间回拨影响）。冷却期内的请求在拿锁前短路，不打认证服务、不等超时、立即降级；刷新成功立即清除冷却。
- **token 响应校验**：先判状态码非 200 直接失败且不读 body，再要求响应体为 dict；`access_token` 为空、上游 `error`、既无 token 也无 error、解析失败、结构异常、网络异常全部走统一失败收口。**续期失败时若旧 token 仍在有效期内则保留**（记 ERROR），首次获取失败才清空 `access_token` 与 `deadline`；明确被告知 token 失效时由 `invalidate()` 清空。
- **有效期换算**：`v = min(float(上游字段), max_expires_in)`，`ttl = min(max(v - refresh_buffer, min_lifetime), v)`。`ttl` 永不超过 `v`，deadline 始终落在未来；`TokenState.deadline` 恒为 `datetime`，无有效 token 时为 `datetime.min`。
  - **`600 - 360 = 240s` 不是固定有效期**，只是 `expires_in` 缺字段 / 非法值回落到 `default=600` 时的结果。上游给 `600000`（秒）时 `max=604800` 不截断，`ttl=599640s ≈ 6.94 天`。
  - 字段名按 `IOP_EXPIRES_IN_FIELDS` 候选取（命中别名记 INFO）；一个都没有、值非数字 / bool / NaN / 非正 → 回落默认并**记 ERROR**。超过上限被截断、疑似绝对时间戳 / 毫秒也都记日志。
  - 每次刷新成功记 `raw_expires_in` 与本地 `ttl`，排查有效期不必再猜单位。
- **统一降级 + 可区分**：token 不可用、超时 / 网络异常、响应解析或校验失败、标签查询失败全部返回 `{}` 不抛异常（API 不 500），但 `with_status` 会返回具体 `status`（`token_unavailable` / `network_error` / `parse_error` / `http_error` / `bad_structure` / `business_error` / `missing_busiinfo` / `ok_empty`）；`ok` 与 `ok_empty` 表示上游成功。`IopLabelProvider` 把降级原因记进在线日志，并注明「无辅助画像 + 切入/挽留回落兜底组」。
- **日志**：由构造时注入的 logger 输出（未注入则完全静默），字段为 `key=value`。在线进程 `configure_logging()` 的 INFO 文件 sink 同时收 `INFO` / `WARNING`，ERROR 单独一个文件。日志不含手机号与 `access_token` 值；请求级降级日志只带 `telnum_len`。

## 6. 排查「同样查询一次有辅助画像、一次没有」

代码里两条「极快就返回空标签」的路径都不会超时：

1. **冷却短路（0 次 HTTP）**：上一轮 token 刷新全失败后 `IOP_RETRY_COOLDOWN=5` 秒内，`ensure_token()` 拿锁前直接返回，`get_customer_labels_with_status()` 返回 `token_unavailable`。日志有 `IOP token 处于失败冷却期` 与 `IOP 标签为空 ... reason=token_unavailable`。
2. **标签端点认证类快速失败（1 次 HTTP，几十 ms）**：两个部署目录共用同一 `client_id` 时，一方刷新会把另一方的 token 顶掉，上游返回 `HTTP 200 + res_code=1001 / access_token invalid`。客户端会自动 invalidate + 重刷 + 重试一次，日志有 `IOP 标签查询疑似 token 失效` → `IOP token 失效自动重刷成功`。

**先抓一次 token 端点原始响应，确认 `expires_in` 的字段名与单位**：

```bash
python tests/check_iop_token_raw.py            # 只读 .env + 发一次真实 token 请求
python tests/check_iop_offline.py              # 离线自检：不打上级，覆盖全部修复点
python tests/check_iop_direct.py <电话号>      # 直连标签查询，打印降级 status
```

`check_iop_token_raw.py` 会打印打码后的原始响应、命中的有效期字段、按「秒」换算的本地 `ttl`，并在值像毫秒 / 绝对时间戳时给出提示；`IOP/.env` 的 `IOP_EXPIRES_IN_FIELDS` / `IOP_LIFETIME_MAX_EXPIRES_IN` 要按实际响应调整，**改完必须重启进程**（`IOPConfig` 构造时读一次并冻结）。

线上日志位置是项目根 `log/INFO_*.log` / `log/ERROR_*.log`（不是终端）。检索关键词：`IOP 降级`、`token 失效`、`冷却期`、`缺少有效期字段`、`raw_expires_in`、`reason=`。
