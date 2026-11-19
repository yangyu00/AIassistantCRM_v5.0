# IOP · 用户标签查询客户端

`IOP/` 是用户标签（画像）查询客户端，公开入口只有 `IOPClient`：

```python
from IOP import IOPClient

async with IOPClient() as client:
    labels = await client.get_customer_labels("13900000000")
# 返回 {ITEMCODE: [ITEMNAME, ITEMVALUE], ...}
```

`get_customer_labels(tel)` 是唯一业务入口。**任何失败统一降级为 `{}`，不返回 `None`**：`None` 不会触发上层 `except`，会一路传到上层业务对象并抛 `TypeError`，反而让最该降级的场景返回 500。成功但 0 标签也返回 `{}`，靠日志区分。

> 本模块只负责「取标签 / 统一降级 `{}`」，不感知业务路由与产品 ID。在线侧
> `main.py` 如何共享客户端、按业务声明是否查询、以及标签如何进入业务对象，
> 属于集成层逻辑，统一记在[根目录 README](../README.md)的「请求处理与路由 →
> 在线侧 IOP 集成」，本文件不重复。

## 模块划分

| 文件 | 职责 |
|---|---|
| `config.py` | 唯一配置来源：运行参数、冻结配置对象、数值容错 |
| `endpoints.py` / `endpoints_spec.py` | 两个端点的请求契约（URL / 参数 / 请求头）及配置适配 |
| `transport.py` | `aiohttp` 共享会话与响应收口，`Transport` 协议可注入替身 |
| `token.py` | token 刷新原语、单飞、重试退避冷却、响应校验、有效期 |
| `labels.py` | 标签响应的逐层校验与 `BUSIINFO` 组装（纯函数） |
| `client.py` | 业务门面，编排「确保 token → 请求 → 校验 → 降级」 |

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

契约值集中在 `endpoints.py`；新增 IOP 接口时在该文件加一个 `*_request()` 即可，调用点不需要知道路径与参数名。

## 配置（唯一来源：`IOP/config.py`）

全部参数以模块级常量定义，**不读 `config.yaml`、不读环境变量、不依赖项目其它模块**。`IOPClient()` 默认取 `IOPConfig.from_module_defaults()`；需要换地址、凭据或策略时用 `IOPConfig.from_mapping({...})` 显式覆盖，分组为 `TOKEN` / `CustInfo` / `timeout` / `retry` / `token_lifetime`。配置对象冻结，改参数需重启进程。

| 组 | 参数 | 默认值 |
|---|---|---|
| `timeout` | `total` / `connect` / `sock_read` | `3` / `1` / `2` 秒 |
| `retry` | `max_attempts` / `backoff_base` / `backoff_factor` / `backoff_max` / `cooldown` | `4` / `0.2` / `2` / `2` / `5` 秒 |
| `token_lifetime` | `default_expires_in` / `refresh_buffer` / `min_lifetime` / `max_expires_in` | `600` / `360` / `60` / `86400` 秒 |

数值容错：缺键、类型错、NaN / inf、越界一律回落默认；`max_attempts` 上限 20，`cooldown=0` 表示不冷却。端点 host / port / version / type 与生产凭据也在本文件，`IOPConfig.__repr__` 屏蔽 `secret`；仓库公开分发前应改由调用方通过 `from_mapping` 注入凭据。

## 并发与可靠性约束

- **共享会话**：`AiohttpTransport` 只创建一个 `aiohttp.ClientSession`，`start()` / `aclose()` 对应应用 lifespan 的 startup / shutdown，幂等且异常不外抛；session 绑定创建它的 event loop，**必须在同一 loop 内关闭**。未经 lifespan 时（独立脚本、TestClient）降级为「本次请求一个临时 session」，结果正确、不泄漏连接，但失去连接复用。
- **token 单飞**：`ensure_token()` 是唯一入口；`token2global()` 仅作为单次刷新原语在锁内调用，业务侧不得直接调用。`asyncio.Lock` 按当前 event loop 懒创建/重建；「刷新代次」让同一批并发共享一轮结果（含失败），避免 N 个请求各刷一轮打爆认证服务。
- **重试、退避与冷却**：单轮最多 `max_attempts` 次请求，失败间按 `min(base × factor^(k-1), max)` 指数退避；全失败后进入 `cooldown` 冷却，截止点用 `time.monotonic()`（不受系统时间回拨影响）。冷却期内的请求在拿锁前短路，不打认证服务、不等超时、立即降级；刷新成功立即清除冷却。
- **token 响应校验**：先判状态码非 200 直接失败且不读 body，再要求响应体为 dict；`access_token` 为空、上游 `error`、既无 token 也无 error、解析失败、结构异常、网络异常全部走统一失败收口，清空 `access_token` 与 `deadline`，**不沿用可能已过期的旧 token**。
- **有效期换算**：`ttl = min(max(v - refresh_buffer, min_lifetime), v)`，`v` 为上游 `expires_in`（非数字 / bool / NaN / 非正 → 回落默认，超过 `max_expires_in` → 截断）；`ttl` 永不超过上游声明值，deadline 始终落在未来。`TokenState.deadline` 恒为 `datetime`，无有效 token 时统一为 `datetime.min`。
- **统一降级**：token 不可用、超时/网络异常、响应解析或校验失败、标签查询失败全部返回 `{}`，不抛异常，API 不 500；token 侧失败不沿用旧 token。
- **日志**：由构造时注入的 logger 输出（未注入则完全静默），字段为 `key=value`，只记 INFO / ERROR；**不含手机号与 `access_token` 值**。
