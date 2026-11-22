# IOP · 用户标签查询客户端

`IOP/` 是用户标签（画像）查询客户端，公开入口只有 `IOPClient`：

```python
from IOP import IOPClient

async with IOPClient() as client:
    labels = await client.get_customer_labels("13900000000")
# 返回 {ITEMCODE: [ITEMNAME, ITEMVALUE], ...}
```

**日常只改 `IOP/.env`**：地址、凭据、超时、重试、有效期都在那一个文件里，改完重启进程即可。`import IOP` 不读任何文件，配置在构造客户端时才加载并冻结。

`get_customer_labels(tel)` 是唯一业务入口。**任何失败统一降级为 `{}`，不返回 `None`**：`None` 不会触发上层 `except`，会一路传到上层业务对象并抛 `TypeError`，反而让最该降级的场景返回 500。成功但 0 标签也返回 `{}`，靠日志区分。

> 本模块只负责「取标签 / 统一降级 `{}`」

## 模块划分

依赖是单向的，排查问题按这个顺序看：`config` → `endpoints` → `transport` / `token` / `labels` → `client`。

| 文件 | 职责 |
|---|---|
| `config.py` | 读 `IOP/.env`、默认值与数值容错、冻结配置对象（配置层就这一个文件） |
| `endpoints.py` | 上游请求契约：两个端点的 URL / 参数 / 请求头，**两个纯函数**，按传入的 `IOPConfig` 现算 |
| `transport.py` | `aiohttp` 共享会话与响应收口，`Transport` 协议可注入替身 |
| `token.py` | token 刷新原语、单飞、重试退避冷却、响应校验、有效期 |
| `labels.py` | 标签响应的逐层校验与 `BUSIINFO` 组装（纯函数） |
| `client.py` | 业务门面，编排「确保 token → 请求 → 校验 → 降级」 |

`client.py` 与 `token.py` 都直接调用 `endpoints.py` 的纯函数。打桩 / 换实现
只需注入 `Transport` 一种替身，不用同时替换请求形状和会话。

## 请求链路

1. `ensure_token()`：token 有效则零开销直接返回；否则在单飞锁内完成一轮刷新。
2. 刷新后仍无有效 token，直接返回 `{}`，不发标签请求。
3. 标签查询用 `json=` + 显式 headers 发出；建连/读超时与网络异常就地收口并返回 `{}`。
4. `parse_custinfo_response()` 依次校验：`status == 200`、响应体为 dict、`res_code` 按字符串比较等于 `"0"`、`result.BUSIINFO` 为 list；单条缺 `ITEMCODE` 或非 dict 的记录跳过，不拖累整批。任一环节失败返回 `{}`。

## 上游既定约定（不可改动）

- 两个端点只提供 `http` 明文；token 端点固定 `grant_type=client_credentials`，`client_id` / `client_secret` 以 `urlencode` 表单串提交。
- `access_token` 必须放在标签查询 URL 的查询串，上游不接受 `Authorization` 头。
- 标签请求头必须显式声明 `Content-Type: application/json; charset=gbk` 与 `Accept-Encoding: gzip, deflate`，即使与实际序列化编码不一致也照发；响应解析用 `json(content_type=None)`，不做 Content-Type 类型校验。
- token 请求用字符串 `data=`，**不带 Content-Type**。共享 session 统一带 `skip_auto_headers=("Content-Type",)`，只跳过自动生成的头，显式传入的 `charset=gbk` / `Accept-Encoding` 原样发出。

契约值集中在 `endpoints.py`；新增 IOP 接口时在该文件加一个 `*_request(cfg, ...)` 即可，调用点不需要知道路径与参数名。

## 配置

**唯一来源 `IOP/.env`，唯一入口 `IOPConfig.load()`。** 不读系统环境变量，没有
`IOP_ENV_FILE` 路径覆盖，也没有 `from_mapping()` / `from_module_defaults()`
这类覆盖入口。改完 `.env` **重启进程**生效（`IOPClient()` 构造时读一次，结果冻结）。

| 文件 | 作用 |
|---|---|
| `IOP/.env` | 与 `config.py` 同目录，不依赖启动时的当前工作目录，日常改这个 |
| `IOP/.env.example` | 带注释的键说明模板（键含义 / 默认值 / 约束），**不替代实际 `.env`**；`.env` 按当前约定随仓库入库，新环境初始化时可 `cp IOP/.env.example IOP/.env` |
| `IOP/config.py` 的 `_SPECS` | 20 个键的清单、默认值与数值下界；新增参数在这里加一行并写进 `.env.example` |

`.env` 语法只认**整行 `#` 注释**和 `KEY=VALUE`，不做引号、行内注释或
`${VAR}` 插值——值里要带空格 / `#` 就别用，保持一眼能看懂。

想确认改没生效（配置对象冻结，`repr` 会自动屏蔽 `secret`）：

```bash
python -c "from IOP import IOPConfig; print(IOPConfig.load())"
```

### 键表（完整注释见 `IOP/.env`）

| 分组 | `.env` 键 | 默认值 |
|---|---|---|
| token 端点 / 凭据 | `IOP_TOKEN_HOST` / `IOP_TOKEN_PORT` / `IOP_TOKEN_ID` / `IOP_TOKEN_SECRET` | `134.80.220.55` / `80` / 见 `.env` / 见 `.env` |
| 标签端点 | `IOP_CUSTINFO_HOST` / `IOP_CUSTINFO_PORT` / `IOP_CUSTINFO_VERSION` / `IOP_CUSTINFO_TYPE` | `134.80.220.55` / `80` / `1.0.1` / `M` |
| 超时（秒） | `IOP_TIMEOUT_TOTAL` / `IOP_TIMEOUT_CONNECT` / `IOP_TIMEOUT_SOCK_READ` | `3` / `1` / `2` |
| 重试 / 退避 / 冷却 | `IOP_RETRY_MAX_ATTEMPTS` / `IOP_RETRY_BACKOFF_BASE` / `IOP_RETRY_BACKOFF_FACTOR` / `IOP_RETRY_BACKOFF_MAX` / `IOP_RETRY_COOLDOWN` | `4` / `0.2` / `2` / `2` / `5` |
| token 有效期（秒） | `IOP_LIFETIME_DEFAULT_EXPIRES_IN` / `IOP_LIFETIME_REFRESH_BUFFER` / `IOP_LIFETIME_MIN_LIFETIME` / `IOP_LIFETIME_MAX_EXPIRES_IN` | `600` / `360` / `60` / `86400` |

### 容错与安全

- **缺键 / 数值填错不报错**：回落 `config.py` 里的内置默认值。非数字、NaN / inf、
  低于下界一律回落；`max_attempts` 硬上限 20（不开放配置，防止配错导致长时间
  串行循环），`cooldown=0` 表示不冷却，`IOP_TIMEOUT_*` 填 0 或过小值回落默认，
  避免请求立刻超时。文件不存在时同理，全部回落默认值。
- **没有凭据时直接回落**：`.env` 缺文件、缺凭据键，或值仍是 `<填入 ...>` 占位符
  （`.env.example` 只是键注释模板、不替代 `.env`），都直接回落到 `config.py`
  里的明文凭据，不会把占位符当成真实配置。
- **凭据**：`IOPConfig.__repr__` 屏蔽 `secret`；`IOP/.env` 含明文凭据，按当前约定
  随仓库入库、不进 `.gitignore`。部署时按实际环境替换凭据；对外分发本仓库前
  请评估凭据暴露风险。

## 并发与可靠性约束

- **共享会话**：`AiohttpTransport` 只创建一个 `aiohttp.ClientSession`，`start()` / `aclose()` 对应应用 lifespan 的 startup / shutdown，幂等且异常不外抛；session 绑定创建它的 event loop，**必须在同一 loop 内关闭**。未经 lifespan 时（独立脚本、TestClient）降级为「本次请求一个临时 session」，结果正确、不泄漏连接，但失去连接复用。
- **token 单飞**：`ensure_token()` 是唯一入口；`refresh_once()` 仅作为单次刷新原语在锁内调用，业务侧不得直接调用。`asyncio.Lock` 按当前 event loop 懒创建/重建；「刷新代次」让同一批并发共享一轮结果（含失败），避免 N 个请求各刷一轮打爆认证服务。
- **重试、退避与冷却**：单轮最多 `max_attempts` 次请求，失败间按 `min(base × factor^(k-1), max)` 指数退避；全失败后进入 `cooldown` 冷却，截止点用 `time.monotonic()`（不受系统时间回拨影响）。冷却期内的请求在拿锁前短路，不打认证服务、不等超时、立即降级；刷新成功立即清除冷却。
- **token 响应校验**：先判状态码非 200 直接失败且不读 body，再要求响应体为 dict；`access_token` 为空、上游 `error`、既无 token 也无 error、解析失败、结构异常、网络异常全部走统一失败收口，清空 `access_token` 与 `deadline`，**不沿用可能已过期的旧 token**。
- **有效期换算**：`ttl = min(max(v - refresh_buffer, min_lifetime), v)`，`v` 为上游 `expires_in`（非数字 / bool / NaN / 非正 → 回落默认，超过 `max_expires_in` → 截断）；`ttl` 永不超过上游声明值，deadline 始终落在未来。`TokenState.deadline` 恒为 `datetime`，无有效 token 时统一为 `datetime.min`。
- **统一降级**：token 不可用、超时/网络异常、响应解析或校验失败、标签查询失败全部返回 `{}`，不抛异常，API 不 500；token 侧失败不沿用旧 token。
- **日志**：由构造时注入的 logger 输出（未注入则完全静默），字段为 `key=value`，正常路径只记 INFO / ERROR；唯一例外是「共享会话未初始化、降级为临时 session」记一条 WARNING，且一个实例只记一次。**不含手机号与 `access_token` 值**。
