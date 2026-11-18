# IOP 模块重构方案（审核定稿 · 已并入全部决议，本轮不改任何代码）

> 目标：把 IOP 从「只能长在本项目里的全局函数集」变成「可单独测试、可被其他项目复用的面向对象包」。
> 原则：**行为等价、小步增量、旧接口不断、每步可回退**。本文件只是方案，你确认后才会动代码。

## 审核决议台账

| 轮次 | 决议 | 落地位置 |
|---|---|---|
| 1 | **IOP 的所有配置在 IOP 模块内直接定义，不从外部导入** | §4.3 |
| 1 | **确定创建 `IOPClient`，无视以前的规定**（解除 `Missions_reader.md:1055`） | §二、§4.2 |
| 2 | **根 `config.yaml` 的 `IOP` 段：可以** → 按推荐：第 0–4 步原样保留 + 加一行「已迁至 `IOP/config.py`，此处不再生效」注释；第 5 步或独立发版时删除 | §六 |
| 2 | **日志：创建对象时注入，未注入则不记录日志** → IOP 包内（含兼容层）**不再 import `src.log_util`**，间接外部依赖彻底切断；默认 `logger=None` → 完全静默 | §4.4、§4.5、§五.9 |
| 2 | **无需 pytest 用例** → 不新增 `tests/` 目录、不引入 pytest 依赖、不落测试文件 | §六「验证方式」 |

> 对第 2 轮第 2 条的理解：按「未注入则不记录日志」，`logger` 是**可选**参数（不是必填）；注入才记，不注入即静默。若你本意是「必填、不传就报错」，下一轮说一声即可改。

---

## 一、代码读取结果（tarball / codeload）

按要求先试常规路径，已成功，legacy 路径未启用：

```
GET https://codeload.github.com/yangyu00/AIassistantCRM_v5.0/tar.gz/refs/heads/master
→ HTTP 200，1.4 MB gzip，解压到 AIassistantCRM_v5.0-master/
```

备用路径 `https://codeload.github.com/yangyu00/AIassistantCRM_v5.0/legacy.tar.gz/master` 本次无需使用。

**一个需要留意的差异**：你提供的 GitHub 仓库页面快照与 tarball 里的 master 实际内容不一致。

| 页面快照（`init` / cb6f381） | tarball 里的 master |
|---|---|
| `Missions.md` | `Missions_reader.md` + `Missions_writer.md` |
| `sftp_workdir_f09554 / _f09591 / _f09609` | 已删除，改为 `tasks/` |
| 无 `.env` | 有 `.env` |
| 1 个 commit | 目录结构已是重构后形态 |

后续一切以 **tarball 解出来的实际代码** 为准。方案里引用的行号、符号均来自这份代码。

### IOP 现状盘点

`IOP/` 共 4 个文件、约 602 行，**没有 `__init__.py`**（靠 Python 3 隐式命名空间包工作），而 `tasks/`、`excel2consts/`、`services/` 都有 `__init__.py`。

| 文件 | 行数 | 承担的职责 |
|---|---|---|
| `endpoints.py` | 57 | 两个端点的 URL / 查询串 / 表单 / 请求头构造；契约常量集中地 |
| `http_session.py` | 121 | `global_config` 单例、`client_timeout()`、共享 `ClientSession` 的创建/关闭/降级 |
| `get_token.py` | 333 | `TOKENS` 单例、重试/退避/冷却参数、token 单飞（锁 + 代次）、有效期换算、单次刷新与响应校验 |
| `get_custinfo.py` | 91 | 编排：确保 token → 查标签 → 逐层校验 → 降级 `{}` → 解析成标签 dict |

### 当前外部耦合（正是「不好单独测试 / 不好复用」的根因）

1. **配置**：`http_session.py` 在 import 时执行 `global_config = ImmutableYamlConfig()`，单例、路径写死为项目根 `config.yaml`。IOP 只用到 `cfg['IOP']` 一段，却要拖着整份项目配置。**决议 1 消除的就是这一条。**
2. **日志**：所有模块直接 `from src.log_util import my_log`；该模块 import 时创建 `log/` 目录并挂两个 loguru sink，且代码注释明确依赖「只落盘 INFO/ERROR、WARNING 不落盘」的隐性规则。**决议 2 改为构造时注入。**
3. **模块级可变全局**：`TOKENS`、`_SESSION`、`_LOOP`、`_WARNED`、`_COOLDOWN_UNTIL`、`_COOLDOWN_WARNED_UNTIL`、`_REFRESH_LOCK`、`_REFRESH_LOCK_LOOP`、`_REFRESH_GEN`、`RETRY`、`LIFETIME` → 同一进程里放不下两套配置，测试间状态互相污染。
4. **传输与会话绑死**：`session_context()` 直接构造 `aiohttp.ClientSession`，无法用纯内存假响应覆盖失败分支。
5. **调用方**：`main.py` import `get_custom_info_in_global` / `init_session` / `close_session`；`test_local/test_iop.py` 直接 import 4 个模块的符号。

---

## 二、与既有决策的冲突（已解除）

`Missions_reader.md` 第 1055 行原记录「**`IOPClient` 类：已决定不做**」，理由是并发正确性已由单飞 + 代次解决、类化要同时改 4 个文件、风险收益比不划算。

**已确认无视该记录，创建 `IOPClient`**，但保留它当年指出的风险并逐条设防：

- 当年的目标是「并发正确性」，这次是「**实例隔离 + 配置自包含 + 依赖注入 + 可独立分发**」，模块级全局恰好是硬障碍。
- 用「**新类并存 + 旧壳不动 + 影子对照**」绕开「同时改 4 个文件」：新增文件承担新能力，旧 4 个文件最后才退化成薄适配，`main.py` / `test_local` 全程零改动，并发逻辑**原样平移函数体**，不重写。
- 第 4 步同步修订 `Missions_reader.md` 该段落，避免文档与代码长期打架。

---

## 三、目标边界

### 本轮交付

只有这份方案。不改任何生产代码、不改 `config.yaml`、不改请求契约、不新增测试文件。

### 重构完成后要达到

| 能力 | 现状 | 目标 |
|---|---|---|
| 配置 | import 时读项目根 `config.yaml` 的 `IOP` 段 | **全部定义在 `IOP/config.py` 内，核心层零外部依赖；`IOPClient()` 零参数可用** |
| 日志 | 强依赖 `src.log_util`，import 即建 `log/` | **构造时注入；未注入 = 零日志输出**，IOP 包内无任何 `src.log_util` import |
| 会话 | 模块级全局 session | 实例自持，或注入外部 session / 假传输层 |
| token 状态 | 模块级单例 | 实例内，每个用例一个全新实例 |
| 测试 | 需打桩模块全局或真实联网 | 可注入假传输层，纯内存、无网络、无文件副作用（**不落 pytest 用例**） |
| 其他项目使用 | 必须带项目根 `config.py` / `config.yaml` / `src/` | 拷走 `IOP/` 目录即可 `async with IOPClient()` |
| 本项目调用方 | — | **零改动**，返回值与请求行为逐条对齐 |

### 明确不做

- 不改上游契约：http 明文、`access_token` 放查询串、`charset=gbk` / `Accept-Encoding` 请求头、`skip_auto_headers=("Content-Type",)`、`qryCustInfoByChannel` / `format=json` / `grant_type=client_credentials`。文档反复标注「不可改动」，一个字不动。
- 不改任何默认数值（超时 3/1/2、重试 4 次、退避 0.2×2 封顶 2s、冷却 5s、有效期 600/360/60/86400）。
- 不重写并发算法，只把函数体搬进方法、把 `global` 改成 `self.`。
- 不在第一步就删旧文件或改 `main.py`。
- 不新增 `tests/`、不引入 pytest、不把 `FakeTransport` 作为仓库产物保留。

---

## 四、面向对象设计

### 4.1 目标目录（新增文件优先，旧文件最后才动）

```
IOP/
  __init__.py          # 公开导出：IOPClient / IOPConfig / Transport / 兼容符号
  config.py            # 【决议 1】全部配置常量 + 4 个 frozen dataclass + IOPConfig；纯标准库
  endpoints.py         # 保留原函数；新增 EndpointSpec 类封装同一套契约常量
  transport.py         # Transport Protocol + HttpResponse + AiohttpTransport
  token.py             # TokenManager（get_token.py 逻辑平移）
  labels.py            # 标签响应解析（get_custinfo.py 后半段平移）
  client.py            # IOPClient 门面：编排 token → 查询 → 降级
  logging_adapter.py   # NullLogger + Logger Protocol（不 import src.log_util）
  _compat.py           # 进程级默认实例 + 旧符号代理（本项目的适配层，独立分发时不导出）
  # 旧 4 个文件在第 4 步才改成薄壳，之前的 import 全部保持可用
```

**分层界线**（决议 1、2 能否成立的关键）：

| 层 | 文件 | 允许依赖 |
|---|---|---|
| 核心层（可独立分发） | `config.py` / `endpoints.py` / `transport.py` / `token.py` / `labels.py` / `client.py` / `logging_adapter.py` / `__init__.py` | 标准库 + `aiohttp` |
| 兼容层（仅本项目） | `_compat.py` + 旧 4 文件薄壳 | 标准库；**也不 import `src.log_util`** |

> 整个 `IOP/` 目录**禁止**出现 `from config import ...`、`from src.log_util import ...`、`from src.* / services.* import ...`，以及任何读外部配置文件的代码。这样「拷走目录即可用」才成立。

### 4.2 类清单（职责单一，不做上帝类）

| 类 | 职责 | 持有的状态 | 依赖 |
|---|---|---|---|
| `IOPConfig` | 冻结配置对象；`from_module_defaults()` / `from_mapping(overrides)`；缺段、类型错、NaN/inf/越界一律回落默认 | 无（不可变） | **纯标准库，不读文件、不 import 外部模块** |
| `TokenEndpointCfg` / `CustInfoEndpointCfg` | 两组 host/port/凭据/version/type | 无 | — |
| `TimeoutCfg` / `RetryPolicy` / `TokenLifetime` | 三组数值参数与默认值 | 无 | — |
| `EndpointSpec` | 由 `IOPConfig` 生成 `token_request()` / `custinfo_request()` 的 (url, headers, body/post_data) | 无 | 契约常量 |
| `Transport`（Protocol） | 只定义 `async def post(...) -> HttpResponse` | — | — |
| `HttpResponse` | `(status, json)` 的最小结果对象 | 无 | — |
| `AiohttpTransport` | 唯一依赖 aiohttp 的地方：持有 `ClientSession`、`ClientTimeout`、`skip_auto_headers`，`async with` 管理生命周期 | session / loop | aiohttp |
| `TokenManager` | token 值 + deadline、单飞锁、刷新代次、冷却截止点、重试循环、有效期换算、失败清空 | 全部实例内 | `Transport`、`EndpointSpec`、`logger` |
| `NullLogger` | `info` / `error` / `warning` 全为空操作 | 无 | — |
| `IOPClient` | 门面：`get_customer_labels(tel)`；`start()` / `aclose()` / `async with` | 组合上面四者 | 同上 |

构造签名（示意）：

```python
class IOPClient:
    def __init__(self, config=None, transport=None, logger=None):
        # config=None   → IOPConfig.from_module_defaults()（决议 1）
        # transport=None→ AiohttpTransport(...)
        # logger=None   → NullLogger（决议 2：不注入就不记录日志）
```

示例用法（**示意，非本轮落地代码**）：

```python
# 其他项目：配置在模块内、日志默认静默，零参数即可
async with IOPClient() as client:
    labels = await client.get_customer_labels("13900000000")

# 想要日志：创建时注入（由调用方决定，IOP 不替你决定）
async with IOPClient(logger=my_log) as client:
    ...

# 换地址 / 换凭据：调用方显式覆盖（不是模块去外部读）
cfg = IOPConfig.from_mapping({"TOKEN": {"host": "10.0.0.1", ...}})
```

> `from_mapping()` 与决议 1 不冲突：**默认路径**完全取自模块内常量、零外部依赖；覆盖是调用方主动传进来的值。

### 4.3 配置自包含（决议 1 的落地设计）

**取消**原方案里的 `from_yaml()` / 读 `ImmutableYamlConfig()['IOP']` / 环境变量注入。改为：

1. `IOP/config.py` 内以模块级常量**直接定义**全部配置项，集中在一处、带注释，逐项对应现有 `config.yaml` 的 `IOP` 段：

   | 分组 | 键 | 来源 |
   |---|---|---|
   | `TOKEN` | `host` / `port` / `id` / `secret` | 根 `config.yaml:18-23` 原值搬迁 |
   | `CustInfo` | `host` / `port` / `version` / `type` | 根 `config.yaml:24-28` 原值搬迁 |
   | `timeout` | `total=3` / `connect=1` / `sock_read=2` | 根 `config.yaml:30-35`，同时是代码兜底默认值 |
   | `retry` | `max_attempts=4` / `backoff_base=0.2` / `backoff_factor=2` / `backoff_max=2` / `cooldown=5` | 根 `config.yaml:37-46` + `_DEFAULT_RETRY` |
   | `token_lifetime` | `default_expires_in=600` / `refresh_buffer=360` / `min_lifetime=60` / `max_expires_in=86400` | 根 `config.yaml:48-55` + `_DEFAULT_LIFETIME` |

   > 凭据值从现有 `config.yaml` **原样搬迁**，本方案文件不抄录明文 `id` / `secret`。

2. 4 个 `frozen dataclass` 的字段默认值直接引用上述常量；`from_module_defaults()` 不读盘、不 import 任何项目模块。
3. 现有三处配置容错（`_retry_profile()` / `_lifetime_profile()` / `client_timeout()`）的校验规则**原样平移**：NaN / inf / 负数 / 零 / 类型错 / `max_attempts` 上限 20 一律回落默认。
4. `IOPConfig.__repr__` 屏蔽 `secret`；示例与日志不打印凭据。
5. `IOPClient()` 零参数可用。

### 4.4 日志注入（决议 2 的落地设计）

1. **注入点**：`IOPClient(..., logger=None)`。logger 只需满足最小协议 `info(msg)` / `error(msg)` / `warning(msg)`——`loguru` 的 `my_log`、`logging.Logger`、自定义对象都能直接传，不需要包装类。
2. **未注入 → `NullLogger`**：三个方法全为空操作。所有原本的 `my_log.info/error/warning` 调用改成 `self._logger.info/error/warning`，级别与文案**逐字保留**，只是不输出。
3. **零副作用**：因为不再 import `src.log_util`，IOP 包被 import 时**不创建 `log/` 目录、不挂 sink、不读根 `config.yaml`**。间接外部依赖彻底切断。
4. **注入由调用方负责**：本项目的 `main.py` 若想恢复 IOP 日志，在自己的启动代码里写 `IOPClient(logger=my_log)` 或调一行 `_compat.configure_logger(my_log)`（该口子保留，默认不调用）。IOP 内部不替调用方决定。
5. 原代码里「WARNING 不落盘所以冷却用 INFO、重试用 ERROR」的注释保留为历史说明；级别不因重构改变，注入后仍按项目规则可见。

### 4.5 兼容层设计（不引入未知 bug 的关键）

`_compat.py` 维护**一个进程级懒创建默认实例**：

```
_default_client() = IOPClient(
    config=IOPConfig.from_module_defaults(),   # 决议 1：不读 ImmutableYamlConfig()['IOP']
    transport=AiohttpTransport(...),
    # 不传 logger → NullLogger → 静默（决议 2）
)
```

旧 4 个文件最终变成薄壳：

| 旧符号 | 去向 |
|---|---|
| `get_custom_info_in_global(tel)` | `await _default_client().get_customer_labels(tel)` |
| `init_session()` / `close_session()` | 默认实例 transport 的 `start()` / `aclose()`（幂等、异常不外抛、同 loop 约束不变） |
| `session_context()` / `get_session()` / `client_timeout()` | 转发到默认实例 transport |
| `TOKENS` | 保留为代理对象，`access_token` / `deadline` 转发到默认实例 `TokenManager` 的 state，仍是 `datetime` 类型 |
| `ensure_token()` / `token_valid()` / `token2global()` / `effective_lifetime()` / `backoff_seconds()` / `RETRY` / `LIFETIME` | 转发到默认实例 `TokenManager` |

`main.py`、`test_local/test_iop.py`、文档里所有自检脚本的 import 路径**全部保持有效**。

> **一处诚实的边界**：默认实例仍是进程级全局，所以「隔离」能力只对**显式构造 `IOPClient`** 的使用方（新项目、验证脚本）生效；兼容层本身不解决多实例隔离，也不宣称解决。
>
> **另一处行为变化**：截至第 4 步，本项目生产侧 IOP 日志为**静默**（详见 §五.9、§七）。这是本次唯一相对现状的行为变化，是你主动选择的结果，不是遗漏。

---

## 五、必须保住的行为不变量（验收基线）

逐条来自现有代码与 `Missions_reader.md`，重构后需逐项对照：

1. **统一降级 `{}`**：token 不可用 / 网络异常 / 超时 / 非 200 / JSON 解析失败 / 响应非 dict / `res_code != "0"` / 缺 `BUSIINFO`，全部返回 `{}`，绝不把异常抛到 `main.py`。
2. **失败不沿用旧 token**：所有失败分支清空 `access_token` 并把 `deadline` 置回 `datetime.min`；`deadline` 恒为 `datetime`。
3. **token 单飞**：`asyncio.Lock` 按当前 event loop 懒创建/重建（跨 `asyncio.run()` 不炸）；拿锁前后二次检查；刷新代次让同批并发共享一轮结果（含失败）；失败冷却在**拿锁之前**短路；一个冷却周期只打一条 INFO。
4. **会话**：共享 session 由 lifespan 在同一 loop 创建/关闭；`init_session` / `close_session` 幂等、异常不外抛；未初始化时降级为本次请求临时 session，只告警一次；两端点共用一个 session，`skip_auto_headers=("Content-Type",)`，token 用字符串 `data=`、标签用 `json=` + 显式头。
5. **有效期换算**：`ttl = min(max(v - refresh_buffer, min_lifetime), v)`，超过 `max_expires_in` 截断，非法值回落默认并记 INFO；**先写 deadline 再写 access_token**。
6. **配置容错**：三处容错校验原样平移到模块内 `IOPConfig`，**默认值仍逐字等于现有 `config.yaml` 的 `IOP` 段**。
7. **请求逐字一致**：同一份配置下，新 `EndpointSpec` 产出的 url / headers / body / post_data 与旧的 `token_request()` / `custinfo_request()` 完全相同；含 `&` / `=` 的 secret 仍由 `urlencode` 正确转义。
8. **零读盘热路径**：配置在模块 import 时由常量确定，请求路径 0 次 I/O（比现状更彻底——现状至少在 import 时读一次 yaml）。
9. **日志（唯一主动变更）**：**注入 logger 时**，前缀、字段、级别与现状逐条一致（`IOP token 请求失败 | status=...`、`IOP 标签查询网络失败 | cost=...ms` 等）；**未注入时零日志输出**。因此 `Missions_reader.md` 936–1034 行里依赖日志文案的自检条目，需先注入 logger 才能复核；其余不变量不受影响。
10. **不含凭据、不含手机号原文**：注入时日志内容不变；未注入时无输出，两条都成立。

---

## 六、分阶段实施（每步都可独立验证、可回退）

| 步 | 内容 | 改动范围 | 验证方式（一次性临时脚本，跑完即删，不进仓库） | 回退成本 |
|---|---|---|---|---|
| 0 | 加 `IOP/__init__.py` + `IOP/config.py`：模块内直接定义全部配置常量 + `IOPConfig` | **仅新增文件**，生产代码 0 行改动 | 把 `IOPConfig.from_module_defaults()` 与根 `config.yaml` 的 `IOP` 段、现有 `RETRY`/`LIFETIME`/`client_timeout()` 逐值比对；缺段/非法值回落一致。**比对时才读根配置做断言，不作为运行时依赖** | 删 2 个文件 |
| 1 | 加 `transport.py`（Protocol + `HttpResponse` + `AiohttpTransport`） | 仅新增 | 新旧两端点请求参数逐字比对（含特殊字符凭据）；本地假 IOP server 跑一次真实 HTTP | 删 1 个文件 |
| 2 | 加 `token.py`（`TokenManager`），函数体平移、全局改 `self.` | 仅新增 | 临时脚本覆盖：单飞成功 10 并发只刷 1 次、token 有效 0 次、失败共享 = `max_attempts` 次、跨 loop、退避序列 0.2/0.4/0.8、冷却、7 类异常响应、有效期 8 组取值 | 删 1 个文件 |
| 3 | 加 `labels.py` + `client.py`（**`IOPClient`**） | 仅新增 | 影子对照：同一批假响应分别喂旧函数与新类，输出 dict 逐条比对；再分别跑「注入 logger / 不注入」两种构造，确认后者零输出 | 删 2 个文件 |
| 4 | 旧 4 文件改薄壳 + `_compat.py` 默认实例；根 `config.yaml` 的 `IOP` 段加一行迁移注释；同步修订 `Missions_reader.md:1055` | **首次修改存量文件** | `main.py` / `test_local/test_iop.py` 一行不改，跑通；lifespan 创建/关闭共享 session 自检；`TOKENS.deadline` 仍是 `datetime`；确认 `IOP/` 全目录无 `src.*` / 根 `config` import | 旧文件从 git 恢复即可 |
| 5 | `main.py` 显式用 `IOPClient`（可在此注入 `my_log`），删旧壳；删除根 `config.yaml` 的 `IOP` 段 | 动在线入口 + 存量配置 | 端到端 + 压测并发 | **风险最高，建议本次不做** |
| 6 | 独立包化：`pyproject.toml`、相对导入、剔除兼容层 | 打包分发 | 空项目里 `pip install -e` 后 `async with IOPClient()` 跑通 | 可单独发版 |

前 4 步任何时候都可以停在任意一步：旧入口一直可用，新类一直可用，互不影响，这就是「不引入未知 bug」的兜底。

> **第 4 步之后会短暂出现两份 `IOP` 配置**（模块内常量 + 根 `config.yaml` 的 `IOP` 段），但只有前者生效；根段按你的「可以」原样保留 + 加注释，第 5 步或发版时删除。第 0–4 步**不动根 `config.yaml` 的实际键值**，避免过早改动部署文件。

### 验证方式（按「无需 pytest」调整）

- **不新增** `tests/` 目录、不引入 pytest、不落任何测试文件。
- 验证用**一次性临时脚本**（写在 `/tmp` 或跑完即删）+ 现有 `test_local/test_iop.py` 手工跑 + 本地假 IOP server。
- `FakeTransport` 只在临时脚本里现写：返回预排好的 `(status, json)` 序列，覆盖成功与 7 类失败分支，**不联网、不建 `log/`、不读任何配置文件**。
- 每个 case 新建一个 `IOPClient` 实例，天然隔离，无需任何全局复位。
- 跨 event loop：连续两次 `asyncio.run()` 各跑一批并发 `ensure_token()`，验证锁按 loop 重建。
- `Missions_reader.md` 936–1034 行已有完整自检清单，直接作为对照用例来源；其中依赖日志文案的条目需注入 logger 后复核。

---

## 七、风险与缓解

| 风险 | 缓解 |
|---|---|
| 类化触碰并发逻辑，引入难以复现的 bug | 函数体**平移**不重写；第 2 步复用文档既有自检清单；新旧影子对照 |
| 兼容层默认实例与显式实例行为分叉 | 默认实例就是 `IOPConfig.from_module_defaults()` 构造的**同一个 `IOPClient`**，没有第二套实现 |
| **生产侧 IOP 日志默认静默**（本次唯一行为变化） | 已按你的决议执行；保留 `_compat.configure_logger(my_log)` 一行恢复口子；第 4 步交付时明确告知；不注入时无输出，反而不会泄露凭据/手机号 |
| **两份 `IOP` 配置漂移** | 第 0 步逐值比对 + 第 4 步起只有模块内常量生效；根段保留但不读取并加注释；第 5 步删除 |
| **凭据明文搬进模块内** | 值从现有 `config.yaml` 原样搬迁，不新增暴露面；`__repr__` 屏蔽 `secret`；独立分发 `IOP/` 前需改成 `from_mapping` 由调用方注入或环境变量，并在 `.gitignore` 标注 |
| `main.py` lifespan 与实例生命周期错位 | 第 4 步 `init_session/close_session` 只转发，同 loop 创建/关闭约束原样保留；第 5 步暂不做 |
| 上游契约被「顺手修好」 | 契约常量集中在 `EndpointSpec`，评审时单独核对一次，写死注释 |
| 包名字母大写 `IOP` 不利分发 | 本次保持原名；若第 6 步发版，分发名另起 `iop-client`，包内仍 `IOP` |

---

## 八、决策点状态

### 已确认（5 条）

1. ✅ 创建 `IOPClient`，无视 `Missions_reader.md:1055` 旧规定。
2. ✅ IOP 全部配置在模块内直接定义，不从外部导入。
3. ✅ 根 `config.yaml` 的 `IOP` 段：第 0–4 步保留 + 加注释，第 5 步/发版时删。
4. ✅ 日志改为创建对象时注入，**未注入则不记录日志**；IOP 包内不含 `src.log_util` import。
5. ✅ 不新增 pytest 用例 / `tests/` / 测试文件。

### 未答复，按推荐执行（无需逐条回复）

6. **类粒度**：采用 `IOPConfig` / `EndpointSpec` / `Transport` / `TokenManager` / `IOPClient` 五类拆分。
7. **本次止于第 4 步**：`main.py` 零改动；第 5/6 步留到以后。
8. **后续顺序**：`IOP` → `tasks` 改名 `sftp_avl2db` → `excel2consts` 改名 `local_excel2db`，IOP 本次不改名。

### 唯一还想请你点一下的开关

9. **第 4 步要不要顺手在生产侧注入 `my_log`**：
   - **推荐（严格按你的决议）**：不注入，IOP 日志静默；想看日志时自己调 `_compat.configure_logger(my_log)` 或等第 5 步。
   - 或：第 4 步在兼容层构造默认实例时注入 `my_log`，生产日志逐条保留——但这会让兼容层重新 import `src.log_util`，与「间接依赖彻底切断」二选一。

> 6–9 若无异议，按推荐执行即可，回一句「按此开始」我就从第 0 步动手。

---

**第 0 步只新增 `__init__.py` + `config.py`，生产代码 0 行改动；每步结束给你「改了哪些文件 + 对照结果」，随时可喊停。**
