# call_stat · 调用量统计

`call_stat/` 是「每次 HTTP 访问按天累计、缓冲落 SQLite、可查询/导出」的独立模块。配置、代码、运行时产物、说明全部锚在包内，按 `__file__` 定位，与启动 CWD 无关；其它项目 copy 本目录即可复用。

```
call_stat/.env          唯一配置来源
call_stat/runtime/      唯一产物目录（call_sum.db / export/ / selftest/）
Readme.md               本文件（组件自身的唯一说明）
```

`import call_stat` **零 I/O、零第三方依赖**：不读 `.env`、不建目录、不连库、不加载 fastapi；配置在首次调用 `startup()` / `record_call()` 等入口时加载一次并冻结。不读项目根 `config.yaml`，也不读系统环境变量。

> **职责边界**：本文件只描述组件自身（入口、配置、链路、不变量、降级）。`main.py` 如何编排 lifespan、middleware 与路由注册，属于集成层，统一记在[根目录 README](../README.md)的「请求处理与路由 / 调用量统计接入」，本文件不重复。

## 运行

```bash
python -m call_stat --date 2026-11-16                 # 单日
python -m call_stat --month 2026-11                   # 整月每天（补 0）
python -m call_stat --month 2026-11 --export          # 同时导出 CSV
python -m call_stat --flush                           # 把本进程未落库增量写入库（幂等）
python -m call_stat --migrate-from log/call_sum.db    # 停机迁移旧库到 runtime/
python -m call_stat --self-check                      # 自检，临时库在 runtime/selftest/ 用完即删
```

`--date` 与 `--month` 互斥且必须二选一；参数非法返回码 1，运行异常返回码 2，正常 0。`--self-check` / `--migrate-from` / `--flush` 是独立动作，优先于查询执行。

## 目录分层

| 层 | 文件 | 职责 |
|---|---|---|
| 配置 | `config.py` | 读 `.env`、默认值与数值容错、冻结 `CallStatConfig`、**产物路径越界校验**（配置层就这一个文件） |
| 存储 | `store.py` | SQLite 建表 / 读单日 / 累加增量 / 读整月；只做 I/O，不持有全局状态 |
| 计数 | `counter.py` | `RLock` + 两个全局字典；`startup` / `record_call` / `flush` / 查询；`atexit` 兜底 |
| 查询 | `query.py` | 日期/月份严格校验与响应组装（纯函数，`web` 与 `cli` 共用一处口径） |
| 集成 | `web.py` | `count_call_middleware` 与 `build_router()`；fastapi 在函数内延迟导入 |
| 命令行 | `cli.py` / `__main__.py` | `python -m call_stat`：查询、CSV 导出、flush、旧库迁移、自检 |
| 门面 | `__init__.py` | 公共 API 与 `__getattr__` 延迟导出；根级一行接入 |
| 说明 | `Readme.md` | 本文件 |
| 产物 | `runtime/` | `.gitkeep` 占位；运行后生成库、CSV、自检临时目录 |

依赖只有标准库（`sqlite3` / `argparse` / `csv` / `json` / `threading` / `atexit` / `dataclasses` / `pathlib`）。`build_router()` 内部才 `from fastapi import APIRouter`，纯库 / CLI 场景不需要装 fastapi。

## 配置

**唯一来源 `call_stat/.env`，唯一入口 `get_config()`（内部调 `load_config()`）**。语法与 `IOP` / `sftp_avl2db` 一致：只认整行 `#` 注释与 `KEY=VALUE`，不支持引号、行内注释或 `${VAR}` 插值。改完**重启在线进程**生效（首次调用时读一次并冻结）。

| 文件 | 作用 |
|---|---|
| `.env` | 与 `config.py` 同目录，日常改这个；不含任何凭据，可随仓库提交 |
| `.env.example` | 模板，新环境 `cp call_stat/.env.example call_stat/.env` |
| `config.py::_DEFAULTS` | 4 个键的默认值；新增参数在这里加一行并写进模板 |

| `.env` 键 | 默认值 | 约束 |
|---|---|---|
| `CALL_STAT_FLUSH_THRESHOLD` | `100` | 正整数；非数字 / ≤0 / 缺键一律回落 100。调小可缩小强杀时丢失的尾部窗口 |
| `CALL_STAT_QUERY_PATH` | `/api/callStat` | 必须以 `/` 开头且不含空格 / `?`；由 `build_router()` 注册 |
| `CALL_STAT_DB_FILENAME` | `call_sum.db` | **只能是纯文件名**，落在 `runtime/` 下 |
| `CALL_STAT_EXPORT_DIRNAME` | `export` | **只能是纯目录名**，落在 `runtime/` 下 |

**产物边界由代码强制，不靠约定**：`../`、`/tmp/x`、含路径分隔符的值一律被拒绝、打印 WARNING 并回落默认；`configure(db_path=...)`（仅自检注入）也校验目标必须位于 `RUNTIME_DIR` 内。因此库、CSV、自检临时库都不可能写到模块目录外。根目录 `.gitignore` 建议加：

```gitignore
call_stat/runtime/
# call_stat/.env 无凭据；若团队习惯忽略，用 .env.example 复制，缺文件也能按默认值跑
```

部署时不要把 `call_stat/` 整目录覆盖掉；需要把数据挂到独立盘时，可把 `runtime/` 做成软链（路径仍在模块目录内）。

## 存储与缓冲写入

`runtime/call_sum.db`，表 `call_sum(date TEXT PRIMARY KEY, call_count INTEGER NOT NULL DEFAULT 0)`，**一天一条**，一年不到 400 行，不做删除。落库是 `UPDATE call_sum SET call_count = call_count + ?` 的**累加**（当天首次 `INSERT`），不是覆盖，因此重启与多进程都不会互相清零。

进程内两个全局字典（`RLock` 保护）：

| 变量 | 含义 |
|---|---|
| `_DAY_TOTAL[YYYY-MM-DD]` | 当天**完整累计** = 已落库 + 本进程所有请求（无论是否落库）。请求路径只改这个变量，**不读库** |
| `_PENDING[YYYY-MM-DD]` | 其中「尚未落库」的增量 |

流程：

1. 每次请求 `_DAY_TOTAL[today] += 1`、`_PENDING[today] += 1`；返回值直接来自 `_DAY_TOTAL`，热路径完全不碰 SQLite（除非恰好触发 flush）。
2. 某日期 `_PENDING` 累计到 `flush_threshold` 才把这段增量一次性写库，成功后清零；`_DAY_TOTAL` 不受落库影响。
3. **跨天残留**：旧日期不足阈值就翻天时，后续请求顺带重试落库，不会把今天记到昨天，也不丢昨天的尾部。
4. **写库失败**：保留 `_PENDING` 并告警，下一轮重试；`_DAY_TOTAL` 仍准确，异常不抛给正在处理的请求。

**重启后继续计数**：`startup()` 从库读回当天已落库值作为 `_DAY_TOTAL` 基数；即使 lifespan 未执行（TestClient、直接 import 后手工调用），首次 `record_call()` 也会由 `_ensure_day()` 懒加载恢复。例：库里当天已落库 100 → 重启后首次查询返回 100，再发一次请求返回 101。唯一缺口是**强杀/崩溃**：不足阈值、还没落库的那一段会丢（默认最多 99 次），正常关闭（SIGTERM / lifespan shutdown）不丢。

## 生命周期与在线接入

`main.py` 用 FastAPI `lifespan` 编排本模块（不用已弃用的 `@app.on_event`）：

| 阶段 | 动作 |
|---|---|
| startup（`yield` 之前） | `startup()`：建表、恢复当天基数，日志打印「调用量统计已恢复：当天已累计 N 次」 |
| 运行期 | middleware 每次请求调 `record_call()`，纯内存 +1 |
| shutdown（`yield` 之后） | `flush()` 补齐不足阈值的尾部增量 |
| 非 ASGI 兜底 | 模块内 `atexit.register(flush)`；两处幂等，且 `flush()` 在无 pending 时提前返回，**仅 import 不产生任何 I/O** |

接入只需三行，`query_path` 由包内 `.env` 决定，`main.py` 不再引用根配置的 `call_stat` 段：

```python
from call_stat import startup, flush, count_call_middleware, build_router

app = FastAPI(lifespan=lifespan)          # lifespan 内 startup() / flush()
app.middleware("http")(count_call_middleware)
app.include_router(build_router())
```

`uvicorn` 的 SIGTERM 退出路径不经过 `atexit`，所以 lifespan 的 `finally` 必须显式 `flush()`；这一编排责任在 `main.py`，不在本模块。

## GET 查询

路径取自 `CALL_STAT_QUERY_PATH`（默认 `/api/callStat`）。两个 query 参数**必须且只能提供一个**；校验失败返回 `{"res_code": 1, "res_desc": "...", "result": ""}`（HTTP 200，与项目其它接口一致）。

| 参数 | 格式 | 返回 `result` |
|---|---|---|
| `date=2026-11-16` | `YYYY-MM-DD` | `{"date": "2026-11-16", "callCount": 123}` |
| `month=2026-11` | `YYYY-MM` | `{"month": "2026-11", "days": [{"date": "2026-11-01", "callCount": 0}, ...]}` |

```bash
curl "http://host:8989/api/callStat?date=2026-11-16"
curl "http://host:8989/api/callStat?month=2026-11"
```

- 单日与月查询都读到**完整累计**（含尚未落库部分）：本进程有活动的日期用 `_DAY_TOTAL`，历史日期用「库 + `_PENDING`」。
- 月查询按 `calendar.monthrange` 补齐整月每一天，无记录补 `0`，前端可直接画折线。
- 该 GET 自身也经过 middleware 计数，但计数在 `finally` 阶段，所以**本次返回的 `callCount` 不含本次请求**；连续两次查询时第二次会包含第一次。
- 日期/月份用正则 + `strptime` 严格校验，`2026-1-6`、`2026-13` 会被拒绝。

## 降级与容错

| 触发 | 行为 |
|---|---|
| `.env` 缺失 / 缺键 / 数值非法 | 静默或告警后回落内置默认值，**不阻断在线服务**（旁路功能） |
| 产物名写成 `../` 或绝对路径 | 拒绝并回落包内默认值，产物恒在 `runtime/` |
| 启动读库失败 | 只告警、基数按 0 恢复，服务照常启动；库恢复后下次重启从真实值继续 |
| 单日/月查询读库失败 | 降级为 0 / 空 dict，按 0 补齐，不抛 500 |
| 写库失败 | 保留 `_PENDING` 告警、下轮重试；`_DAY_TOTAL` 仍准确 |
| SQLite 瞬时 `disk I/O error` | 有限次退避重试；首次仍用默认 journal，重试连接才把 journal 放内存（**仅该连接生效**，正常文件系统第一次就成功、永不触发）；**只有 commit 成功才返回 True**，重试不会重复累加 |

## CLI 导出与旧库迁移

`--month YYYY-MM --export` 写出 `runtime/export/call_stat_YYYY-MM.csv`（UTF-8 BOM，两列 `date,callCount`），同样落在包内。

从旧 `log/call_sum.db` 迁移（**停机执行**）：

```bash
python -m call_stat --migrate-from log/call_sum.db
```

旧库表结构与累计口径完全不变，命令校验旧库合法后原样复制为 `runtime/call_sum.db`；**目标库已有数据时拒绝执行**，避免把旧累计值重复累加。注意 `log/` 目录仍被 `src/log_util.py` 写 INFO/ERROR 日志，迁移时只挪 `call_sum.db`，不要删目录。重启在线进程后 `startup()` 会读回基数，数字连续。

## 自检

`python -m call_stat --self-check` 在 `runtime/selftest/` 用阈值 3 跑一遍并清理，断言：

- 空库启动基数为 0；连续 7 次调用返回 1..7；第 3、6 次触发落库，库里 6、内存 7；`flush()` 后库里 7、pending 为空。
- 清空内存模拟重启：`startup()` 恢复 7（不是 0），再计一次为 8。
- 月查询补齐整月天数、当天为 8；参数校验拒绝空参/双参/非法日期/非法月份。
- 临时库路径确实位于 `runtime/` 内。

另外 `import call_stat` 后 `runtime/` 不应多出任何文件；`CALL_STAT_QUERY_PATH` 改值后 `build_router()` 注册路径随之变化；`configure(db_path='/tmp/x')` 必须抛 `ValueError`。

## 刻意保真：不要顺手优化

从 `src/call_stat.py` + `main.py` 平移时，以下行为一律不动：

1. 表结构、`UPDATE ... call_count + delta` 累加语义、首次 `INSERT` 分支。
2. `_DAY_TOTAL` / `_PENDING` 双字典语义、阈值 flush、跨天残留重试、写失败保留增量。
3. 热路径不读库；`GET` 返回完整累计但不含本次请求；日期严格校验与 `res_code` 响应形状。
4. lifespan shutdown + `atexit` 双幂等；强杀丢尾部窗口的既有语义。
5. 库里只存累计整数，不做删除、不加索引、不改主键。

## 已知限制

- **CLI 是独立进程**：它看到的是「库 + 本进程 pending」，在线 API 进程内存里尚未落库的尾部增量 CLI 看不到（既有语义，不是 bug）。要查到完整值，等阈值触发、正常关闭后再查，或直接请求 GET 接口。
- 多个进程同时写同一个库（在线 API + CLI `--flush`）靠 `+delta` 累加，不会互相覆盖；但各自的内存基数只在自己进程内准确，这是热路径不读库换来的取舍。
- 本模块只统计「HTTP 访问次数」，不区分接口、状态码与用户，与改造前一致。
