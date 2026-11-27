# IOP 修复说明（随完整项目交付）

本包是 **`master` 分支原始包体**（第一条 `codeload` 取数命中：HTTP 200 / gzip 合法 / 133 个文件）叠加两轮改动后的完整项目：

1. 第一轮：卖点维表话术出口把 `\n` 全部转成 `<br>`（已交付，本包保留，未回退）；
2. 第二轮（本次）：IOP 逻辑修复与可观测性增强。

**对外 API 契约没有变化**：`POST /api/speechQryService` 的请求与响应结构、`code` 判定、四类业务路由、话术 HTML 模板全部保持原样。修复只作用在 IOP 客户端内部与日志上。

---

## 1. 修的是什么

原始现象：两次完全同样的查询，一次没有【辅助画像】、一次有，两次都只要 0.02~0.08 秒，不像超时。

排查后确认的核心问题不是「token 有效期太短」：`600 − 360 = 240s` 只是上游 `expires_in` **缺字段 / 非法值**时的回落值；上游给 600000 秒时，原代码会把它截断到 24 小时后算出 23.9 小时有效期，一天只刷一次。

真正的坑是 **「长有效期 + 标签端点认证失败不触发失效与重刷」**：

- `get_customer_labels()` 只在开头 `ensure_token()` 一次，本地 `deadline` 没到就不刷新；
- 标签端点返回「token 失效」（常见形态 `HTTP 200 + res_code=1001 / access_token invalid`）时，`parse_custinfo_response()` 只记一条日志就 `return {}`；
- **没有任何分支清空 `TokenState` 或强制重刷**，于是进程会带着失效 token 撑完整个 23.9 小时（修复后是 6.94 天），每次查询都是「1 次 HTTP 快速失败 → `{}` → 无辅助画像」；
- 空标签还会让切入 / 挽留话术回落「兜底话术」组，所以两段话术内容也会变；
- 两个部署目录各自持有独立 `IOPClient`，若共用同一 `client_id`，一方刷新会把另一方的 token 顶掉，正好造成「同样入参、一边正常一边降级」。

配套问题：线上 `IOPClient()` 不注入 logger → `NullLogger` 把全部失败原因吞掉；`{}` 把「上游挂了」和「用户真没标签」压成同一种结果；`expires_in` 缺字段静默回落 240s；`max_expires_in=86400` 把 600000 秒硬砍成 24h；token 刷新落在用户请求关键路径；续期刷新失败会无条件清空仍有效的旧 token。

## 2. 改动清单

| 文件 | 改动 |
|---|---|
| `IOP/client.py` | ① 新增 `get_customer_labels_with_status()` 返回 `(labels, status)`；原 `get_customer_labels()` 保持返回 dict 不变 ② 标签端点判定为 token 失效时 `invalidate()` → `ensure_token(force=True)` 跳过冷却短路重刷一次 → 用新 token **重试一次**标签查询 ③ `start()` 启动预热 + 后台续期 task，`aclose()` 取消 ④ 全部失败路径带 `status` 与原因日志 |
| `IOP/labels.py` | 新增 9 个降级状态码（`ok` / `ok_empty` / `token_unavailable` / `network_error` / `parse_error` / `http_error` / `bad_structure` / `business_error` / `missing_busiinfo`）、`is_auth_failure()` 认证失败识别、`parse_custinfo_response_ex()`；原 `parse_custinfo_response()` 向后兼容 |
| `IOP/token.py` | ① `invalidate()` ② `ensure_token(force=True)` ③ `refresh_once()` 返回是否写入新 token（避免「保留旧 token」被误记成刷新成功）④ **续期失败保留仍有效的旧 token** ⑤ `expires_in` 字段名候选 + 缺字段 / 非法值 / 疑似时间戳全部记日志 ⑥ 刷新成功记录上游原始值与本地 `ttl` ⑦ `refresh_due_in()` 供后台续期 |
| `IOP/config.py` | 新增 8 个配置键与 3 个配置对象；`IOP_LIFETIME_MAX_EXPIRES_IN` 默认从 `86400` → `604800`（7 天），600000 秒不再被截断 |
| `IOP/__init__.py` | 导出新状态码 / 纯函数 / 配置类 |
| `IOP/.env`、`.env.example` | 同步新键、修正漏写 `MAX_EXPIRES_IN` 截断的公式注释、上限改 604800 |
| `IOP/README.md` | 请求链路、键表、可靠性约束重写；新增第 6 节「同样查询一次无辅助画像」排查步骤 |
| `main.py` | ① `IOPClient(logger=my_log)` 把 IOP 日志接进在线进程文件 ② `IopLabelProvider` 用 `with_status`，把 `status` 记进日志并区分「号码无标签（INFO）」与「IOP 降级（ERROR）」，注明「无辅助画像 + 切入/挽留回落兜底组」 ③ 日志不含手机号明文 |
| `src/log_util.py` | INFO 文件 sink 同时收 `WARNING`（原来 WARNING 全部丢盘） |
| `tests/check_iop_direct.py` | 注入打印 logger，输出降级 `status` 与 IOP 侧错误原因 |
| `tests/check_diagnosis_offline.py` | 第 7 节在线路由的桩同时覆盖 `get_customer_labels_with_status` |
| `tests/check_iop_offline.py` | **新增**：IOP 修复全量离线自检（不打上级） |
| `tests/check_iop_selfheal.py` | **新增**：端到端回归，验证 token 被顶掉后同样查询仍有【辅助画像】，含关开关的对照组 |
| `tests/check_iop_token_raw.py` | **新增**：抓一次 token 端点原始响应，确认 `expires_in` 字段名 / 值 / 单位 |
| `README.md`、`tests/README.md` | 配置键数、请求编排、降级总表（修正原文「卖点话术不受影响」的错误）、自检清单同步 |

### 新增配置键（都在 `IOP/.env`，改完**必须重启进程**）

| 键 | 默认 | 作用 |
|---|---|---|
| `IOP_EXPIRES_IN_FIELDS` | `expires_in,expiresIn,expire_in` | 有效期字段候选，命中别名记 INFO，都没有记 ERROR |
| `IOP_AUTH_FAILURE_CODES` | `401,403,1001,1002,10001,10102,30001` | 判定 token 失效的业务码 |
| `IOP_AUTH_FAILURE_KEYWORDS` | token / invalid / expired / 认证 / 失效 … | 错误描述关键词 |
| `IOP_AUTH_RETRY_ENABLED` | `1` | 关掉后只记日志不自动重刷（可做对照） |
| `IOP_BACKGROUND_REFRESH_ENABLED` | `1` | 后台续期开关 |
| `IOP_BACKGROUND_REFRESH_LEAD` | `60` | 到期前多少秒触发续期 |
| `IOP_BACKGROUND_REFRESH_MIN_INTERVAL` | `30` | 刷新循环最短间隔 |
| `IOP_WARMUP_TIMEOUT` | `5` | 启动预热最长等待，超时不阻塞启动 |

## 3. 修复后的实际有效期

| 上游返回 | 修复前 ttl | 修复后 ttl |
|---|---|---|
| `expires_in = 600000` | 86040s ≈ 23.9h（被 86400 截断） | **599640s ≈ 6.94 天**（不截断） |
| `expires_in = 3600` | 3240s | 3240s |
| 缺字段 / 非法值 | 240s，**完全静默** | 240s，**记 ERROR 并列出响应所有键** |
| 字段名是 `expiresIn` | 240s（回落到默认） | 按 600000 正常计算，记 INFO |
| 值像绝对时间戳 / 毫秒 | 按秒误判 | 按秒处理但**显式告警**，不自动换算（避免猜错） |

## 4. 验证结果（本环境实跑）

| 脚本 | 结果 |
|---|---|
| `python tests/check_iop_offline.py` | **ALL PASS · 0 项失败**（有效期换算 / 别名 / 缺字段日志 / 认证失效自愈 / 普通失败不误判 / 续期失败保留旧 token / 冷却短路 / 降级状态 / 预热与后台续期 / 向后兼容 / 静态接线，共 40+ 断言） |
| `python tests/check_iop_selfheal.py` | **ALL PASS · 0 项失败**：① 正常查询有画像 ② token 失效自动重刷后仍有画像（刷新 2 次、标签请求 2 次）③ `AUTH_RETRY_ENABLED=0` 对照组确实丢画像 |
| `python tests/check_diagnosis_offline.py` | 第 7 节在线路由**不再 SKIP**，新增桩生效，「整请求仅查 1 次 IOP」「在线侧 script 与独立参照逐字符一致（含 `<br>` 出口）」均 PASS；仍有 **2 项原始包自带的 FAIL**（「兜底话术」空列不回退「标签N」、交集空时切入段不出现），与本修复无关，未改动 |
| `python tests/check_legacy_output_parity.py` | `[SKIP]` 维表库不存在（按脚本设计返回 0），需先跑 `python -m local_excel2db --once` 后再全量复验 |
| 语法编译 | 15 个改动 Python 文件全部通过 |

## 5. 部署与「尽量查到」的排查顺序

1. **先抓一次 token 原始响应**，确认字段名 / 值 / 单位（用户提到 600000 这个量级，也可能是毫秒或时间戳）：
   ```bash
   python tests/check_iop_token_raw.py
   ```
   它会打印打码后的原始响应、命中的有效期字段、本地换算的 `ttl`，并在值可疑时给出提示。按结果调整 `IOP_EXPIRES_IN_FIELDS` / `IOP_LIFETIME_MAX_EXPIRES_IN`。
2. **确认两个部署目录的 `IOP_TOKEN_ID/SECRET` 是否相同**。若相同且上游是单点 token，两边会互相顶掉；即使有自动重刷，也建议把 token 收敛到一处（或分开凭据），否则会反复触发自愈。
3. **改完 `IOP/.env` 必须重启在线进程**（`IOPConfig` 构造时读一次并冻结）。
4. **日志位置**：项目根 `log/INFO_*.log` / `log/ERROR_*.log`（不是终端）。检索：`IOP 降级`、`token 失效`、`自动重刷`、`冷却期`、`缺少有效期字段`、`raw_expires_in`、`reason=`。
5. 直连验证：`python tests/check_iop_direct.py <电话号>`，会打印 IOP 侧日志与 `status`。
6. 三个自检脚本：`check_iop_offline.py`（零依赖）、`check_iop_selfheal.py`（需在线侧依赖）、`check_diagnosis_offline.py`（回归）。

## 6. 仍未改动 / 已知边界

- `IOP_RETRY_COOLDOWN=5` 的冷却短路**保留**（设计如此），但现在每次降级都有日志和 `status`，不再是静默黑洞。
- 两个进程共用凭据导致的互相顶 token 属于部署配置问题，自动重刷能自愈但 会增加上游调用量；彻底解决要收敛 token 来源。
- 若上游认证失败的业务码 / 描述不在默认白名单里，`is_auth_failure()` 不会识别，请把实际码加进 `IOP_AUTH_FAILURE_CODES` 或关键词加进 `IOP_AUTH_FAILURE_KEYWORDS`。
- 原仓库里 2 项既有的「标签0 回退」口径 FAIL，以及 `check_iop_direct.py` 直连会真实消耗一次上游 token，均保持原样。
- 自动重刷**只重试一次**，不会把单次请求拖成 4 次刷新 + 退避。
