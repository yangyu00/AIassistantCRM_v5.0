# AIassistantCRM · 项目说明

本文件是全局规格：系统组成、数据流、对外接口、路由、降级、部署与自检。各组件与业务层的详细契约见文末索引；按这些文档可重建代码。

## 1. 系统组成与运行进程

| 侧 | 包 / 入口 | 进程数 | 职责 |
|---|---|---|---|
| 写入侧 | `python -m sftp_avl2db --task <AVL>` | 12 | 每个 AVL 一个进程：SFTP 拉取 AVL → 解析 → 写 `{AVL}.db` |
| 写入侧 | `python -m local_excel2db` | 1 | 轮询项目根 `excels/*.xlsx` → `dimensions.db` |
| 在线侧 | `python main.py` | 1 | FastAPI 在线 API（默认 `0.0.0.0:8989`，端口由项目根 `.env` 的 `CRM_PORT` 配置），只读业务库与维表，按 `product_id` 拼话术 |
| 在线侧模块 | `call_stat/` | 0（进程内） | HTTP 访问按天计数，缓冲落 `call_stat/runtime/call_sum.db` |

部署时 14 个进程都要起。业务与计数数据的写入边界是：在线进程只写 `call_stat/runtime/call_sum.db`，业务库与维表均由写入侧独立维护；此外在线进程启动时会由 `configure_logging()` 在项目根 `log/` 下写 `INFO_YYYY-MM-DD.log` / `ERROR_YYYY-MM-DD.log`（按天轮转、保留 7 天），这是日志产物，不参与业务数据流。各包配置与运行时产物按 `__file__` 锚在包目录内，与启动 CWD 无关。

依赖清单与安装命令见 [`requirements.txt`](requirements.txt)（`python -m pip install -r requirements.txt`）：Python 3.10+；在线侧 `fastapi>=0.100 / uvicorn / pydantic>=2 / aiohttp / loguru`；写入侧同样需要 `loguru`（`sftp_avl2db/task_logging.py` 的 `TaskLogger` 默认实现，缺它 `python -m sftp_avl2db` 会直接抛 `RuntimeError`、退出码 2），另有 `paramiko`（SFTP）、`openpyxl`（xlsx）、`portalocker`（可选）、`requests`（冒烟脚本）。

**仅支持 Linux / Unix**：两个写入侧进程都依赖 `fcntl` 文件锁（`local_excel2db/watcher.py` 在拿锁时导入；`sftp_avl2db/state.py` 优先用 `portalocker`，缺失时回落到 `fcntl`），部署前先确认目标平台。

`import` 边界并非所有包一致：`sftp_avl2db / local_excel2db / call_stat / services` 四个包根零 I/O、零第三方依赖，靠 `__getattr__` 延迟导出，第三方库在入口函数内延迟加载；但 `IOP` 包根在 `__init__.py` 顶层链式导入 `transport / token / client`，三者模块顶层 `import aiohttp`，因此 `import IOP` 必须先装 `aiohttp`（配置仍只在构造 `IOPClient()` 时才读）；`src/log_util.py` 顶层 `from loguru import logger`、`src/exceptions.py` 顶层 `from fastapi import ...`，二者同样在 import 期就要第三方库。

## 2. 目录结构

| 路径 | 职责 |
|---|---|
| `main.py` | 在线 API：入参校验、IOP 客户端 lifespan、请求级编排、响应组装；顶部常量为服务端口 / 路由 / 日志目录 |
| `src/` | 在线进程日志初始化与自定义异常 |
| `services/` | 在线话术业务层：读取配置、业务库只读、卖点维表只读、五类路由（四类业务 + 兜底）、四类业务话术对象 |
| `IOP/` | 用户标签查询客户端（进程级共享会话，任何失败降级 `{}`） |
| `sftp_avl2db/` | 写入侧：SFTP AVL → SQLite 业务库 |
| `local_excel2db/` | 写入侧：Excel 维表 → SQLite `dimensions.db` |
| `call_stat/` | 调用量统计模块（配置、代码、产物均在包内） |
| `excels/` | `local_excel2db` 唯一输入目录（随仓库提交） |
| `io_samples/` | AVL / IOP 响应 / xlsx 格式样例，仅用于离线复跑与格式对照 |
| `tests/` | 手工冒烟与离线自检脚本（非 pytest 单元测试） |
| `*/.env`、`*/.env.example` | 各组件实际配置与带注释的键模板 |
| `*/runtime/` | 各写入侧 / 统计模块的运行时数据目录（.gitignore 忽略，保留 `.gitkeep`） |

## 3. 数据流

```
SFTP AVL ──> sftp_avl2db ──> runtime/sqlite_db/{project}/{AVL}.db ──┐
                                                                     │
excels/*.xlsx ──> local_excel2db ──> runtime/sqlite_db/dimensions.db ─┤
                                                                     ├──> services/reader.py ──> services/routing.py ──> 产品对象 ──> main.py 响应
IOP 上游 ──> IOPClient（进程级共享）──> 请求级标签 ────────────────────┘        ▲
                                                          services/dimensions.py ─┘（每请求 2 次 mode=ro 连接：selling_points + label_name_mapping）

main.py ──> call_stat/runtime/call_sum.db（在线进程唯一业务写入）
main.py ──> log/INFO_*.log、log/ERROR_*.log（在线进程日志写入，非业务数据）
```

物理布局约定：

- 固定表名 = `{AVL}`，分表名 = `{AVL}_{分表号}`，分表号 `zlib.crc32(str(product_no).encode("utf-8")) % NUM_TABLES`。
- `F09591`（控损）与 `F09633`（诊断·用户场景对应关系）按 `product_no` 分表，默认 128 张；`F09633` 是千万级表，分表键与在线查询键一致，一次请求只命中一张分表。
- 写入侧与在线侧各自独立声明库路径、表字段与分表数，代码 / 配置互不引用；唯一交集是磁盘上的 db 文件。改列顺序、分表数或库路径必须同步两侧声明（写入侧 `sftp_avl2db/table_schemas.py`、`avl_specs.py`、`.env`；在线侧 `services/db_config.py`、`services/.env`）。改分表数须重灌对应 AVL 全量数据。
- 每个 `{AVL}.db` 内另有 `_avl_meta` 批次水位表（写入侧维护、在线侧按同名只读），用于把查询限定在最近 `N` 个 `op_time` 批次；不参与分表与过期清理。
- `F09633` 开启 `<AVL>_DROP_BEFORE_INSERT` 且分表时走双套表原子切换：暂存套表名 `F09633_stage_{分表号}`、上一套 `F09633_old_{分表号}`，在线侧读的当前套仍是 `F09633_{分表号}`；stage 套不建物理主键、纯 `INSERT` 灌完后一次性建非唯一 `product_no` 索引（上游全量 AVL 保证 `(product_no, scene)` 唯一，写入侧不做去重；在线侧对 `pain_points` 按首次出现兜底去重），128 组 RENAME 与 `_avl_meta` 水位推进在同一事务提交，导入失败时当前套不动、在线侧继续读上一日全量数据。触发条件由 `sftp_avl2db/profiles.py::build_task()` 判定：分表 + 全量替换时自动传 `atomic_swap=True`（当前只命中 `F09633`）；磁盘需容纳当前套 + stage 套两份；容量估算见 `sftp_avl2db/sink/sqlite_sink.py` 模块注释（属注释层估算，未验证）。
- 商机、控损、诊断的 AVL 归属与库文件见 [`sftp_avl2db/README.md`](sftp_avl2db/README.md) 与 [`services/README.md`](services/README.md)；平级的卖点话术业务不读任何 AVL 表，只用 `dimensions.db` 里的 `selling_points` 一行。

## 4. 配置来源

| 作用域 | 唯一来源 | 装配入口 | 缺文件 / 缺键行为 |
|---|---|---|---|
| IOP 客户端 | `IOP/.env`（键表见 `IOP/README.md` 第 4 节） | `IOPConfig.load()` | 回落 `config.py` 内置默认值 |
| SFTP 写入 | `sftp_avl2db/.env`（68 必需键 + 8 个可选 `<AVL>_DROP_BEFORE_INSERT` 覆盖键） | `load_profiles()` | 缺任一必需键直接 fail-fast |
| 调用量统计 | `call_stat/.env`（4 键） | `load_config()` | 缺文件告警；缺键静默回落 `config.py` 内置默认值，仅显式写了非法值才告警 |
| 在线读取 | `services/.env`（7 键） | `db_config.load()` | 回落 `db_config.py` 代码内默认值 |
| 维表写入 | `local_excel2db/registry.json` | `load_registry()` | 配置损坏退出码 2 |
| 在线服务 | 项目根 `.env` | `app_config.load_config()` | 端口键 `CRM_PORT`，默认 `8989`；`LOG_DIR`、`MARKET_ASSISTANT_URL` 仍是 `main.py` 顶部常量，端口非法时启动 fail-fast |

**根 `.env` 与各包 `.env` 同语法**：只认整行 `#` 注释与 `KEY=VALUE`，不支持引号、行内注释、`${VAR}` 插值；都不读系统环境变量。仓库随附的 `.env` 为实际生效字符串；`.env.example` 是带注释的键模板，其中 `sftp_avl2db/.env.example` 与同目录 `.env` 的 68 个必需键逐键同值，差异只在可选键（`.env` 另含 8 个 `<AVL>_DROP_BEFORE_INSERT` 覆盖键，见第 10.2 节；另有 2 个临时吞吐配套键 `F09633_SYNC_JOURNAL_OFF` / `F09633_STARTUP_FULL_CHECK`，模板都不写（等价 `0`）、当前 `.env` 分别为 `1` / `0`）；改完配置重启对应进程生效（配置对象首次加载后冻结）。

关键键名：

- `IOP/.env`：`IOP_TOKEN_HOST/PORT/ID/SECRET`、`IOP_CUSTINFO_HOST/PORT/VERSION/TYPE`、`IOP_TIMEOUT_TOTAL/CONNECT/SOCK_READ`、`IOP_RETRY_MAX_ATTEMPTS/BACKOFF_BASE/BACKOFF_FACTOR/BACKOFF_MAX/COOLDOWN`、`IOP_LIFETIME_DEFAULT_EXPIRES_IN/REFRESH_BUFFER/MIN_LIFETIME/MAX_EXPIRES_IN`。
- `services/.env`：`AVL_DB_ROOT`、`DIMENSIONS_DB`、`LOSS_CONTROL_NUM_TABLES`、`F09633_NUM_TABLES`，以及按业务分别配置的新鲜度窗口 `OPPORTUNITY_RECALL_FRESHNESS_WINDOW_DAYS`（默认 `2`）、`LOSS_CONTROL_FRESHNESS_WINDOW_DAYS`（默认 `2`）、`DIAGNOSIS_FRESHNESS_WINDOW_DAYS`（默认 `0`，关闭过滤）。
- `call_stat/.env`：`CALL_STAT_FLUSH_THRESHOLD`（默认 100）、`CALL_STAT_QUERY_PATH`（默认 `/api/callStat`）、`CALL_STAT_DB_FILENAME`（默认 `call_sum.db`）、`CALL_STAT_EXPORT_DIRNAME`（默认 `export`）。
- `sftp_avl2db/.env`：SFTP 公共 5 项 + 全局模式开关 `AVL_DROP_BEFORE_INSERT`（`0` 关闭 / `1` 开启；开启后写入最新 AVL 前按 AVL 重建目标表，其中分表且开启的 `F09633` 走双套表原子切换而非 DROP，见第 3 节；可用 `<AVL>_DROP_BEFORE_INSERT` 覆盖）+ 每个 AVL 的 `<AVL>_BATCH_SIZE / _DATA_EXPIRE_DAYS / _LOCAL_FILE_EXPIRE_DAYS / _POLL_INTERVAL_SECONDS / _PURGE_CYCLE_DAYS`，分表 AVL 另加 `<AVL>_NUM_TABLES`；无项目级键。诊断 8 张表的 `_DATA_EXPIRE_DAYS` 统一为 `365`，商机 / 控损仍为 `3`。另有**可选**的 per-AVL 临时吞吐键 `<AVL>_SYNC_JOURNAL_OFF`（缺省 `0`；`1` = 仅该 AVL 的写入连接改用 `PRAGMA journal_mode=OFF` + `PRAGMA synchronous=OFF`，关闭回滚日志与 fsync），以及配套的 `<AVL>_STARTUP_FULL_CHECK`（缺省 `0`；`1` = 启动自检追加 `PRAGMA quick_check` 全库页校验，8 亿行耗时小时级，仅建议非导入窗口人工体检）。当前仅为 `F09633` 的「约 8 亿条 / 8 小时」全量导入临时置 `SYNC_JOURNAL_OFF=1`，其余 11 个 AVL 不受影响；导入完成后必须改回 `0` 并重启 F09633 写入进程恢复 WAL + NORMAL。因关闭日志带来的全库损坏风险，进程启动时会做库健康检查、判定损坏则**直接删除损坏库（不保留副本）**重建空库并清空 `.fetched` 清单强制重灌（**兜底不等于修复，旧数据会丢，导入前必须另行备份**）；风险清单、检查覆盖范围与恢复步骤见 `sftp_avl2db/README.md` 第 5 节。

**在线服务端口**：唯一来源是**项目根 `.env`** 的 `CRM_PORT`（默认 `8989`），由 `app_config.load_config()` 解析。`main.py` 的 `FASTAPI_CONFIG["port"]` 与 `start_project.sh` 打印的地址都取这一份值，改端口只改这一行、重启进程即生效，不必改代码；与其它包的 `.env` 一样，不读系统环境变量。

```bash
# 项目根 .env：把 8989 改成 9100，重启在线进程即监听 9100
CRM_PORT=8989

python main.py                        # 0.0.0.0:8989
bash start_project.sh                 # 启动横幅显示的地址同步跟随
python -c "from app_config import load_config; print(load_config())"   # 查看当前生效值
```

缺文件告警、缺键静默回落 `8989`；显式写了非整数或超出 `1~65535` 会在启动时抛 `ValueError` 直接退出，不静默回落（端口写错会让服务起在意外端口上）。根 `.env` 不含凭据、无需按 §10.2 替换，`.gitignore` 已放行根目录 `.env` 入库。改了端口后，冒烟脚本要同步设 `CRM_SMOKE_BASE_URL`（见 `tests/README.md`），否则仍打默认 `8989`。

## 5. 在线 API 契约

### POST `/api/speechQryService`

请求体（`Args` / `ProductIds`，pydantic 模型）：

```json
{"sceneId": "", "telnum": "15020868271", "productId": [{"id": "202608041615335173"}]}
```

- `telnum`：非空、11 位数字；`productId`：非空数组，每项 `id` 非空。
- 违反校验统一返回 HTTP 200，分三类，处理器都注册在 `src/exceptions.py::register_errors_handlers()`：
  - `telnumError` → `{"res_code": 1, "res_desc": "电话号输入不正确", "result": ""}`
  - `productIdError` → `{"res_code": 1, "res_desc": "产品ID输入不正确", "result": ""}`
  - 请求体字段类型错误（如 `productId` 传字符串而非数组）触发 `RequestValidationError` → `{"res_code": 1, "res_desc": "参数校验错误", "result": "<str(exc) 原文>"}`，**`result` 非空**。
- 前两类由字段校验器抛出，第三类由 pydantic 抛出；`ProductIds.id` 默认 `""`、`Args.productId` 默认 `[]`，所以「缺 id 字段 / 整个 productId 缺失」会落到 `productIdError`，不会走 `RequestValidationError`。

成功响应：

```json
{
  "res_code": 0, "res_desc": "",
  "result": {"allScripts": [
    {"productId": "...", "productName": "...", "script": "...", "code": "0"}
  ]}
}
```

`code` 按产品对象类型判定：`"0"` 为四类业务对象带话术；`"2"` 为兜底对象，`productName` 与 `script` 均为空。不能按 `product_name` 是否为空判定。

### GET `/api/callStat`

- 参数 `date=YYYY-MM-DD` 或 `month=YYYY-MM`，必须且只能提供一个；格式非法返回 `res_code: 1`。
- 单日返回 `{"date": "2026-11-16", "callCount": 123}`；整月返回 `{"month": "2026-11", "days": [{"date": "2026-11-01", "callCount": 0}, ...]}`（补齐整月）。
- 该 GET 自身也计入调用量，但计数在 `finally` 阶段，本次返回值不含本次请求。

## 6. 请求编排（`main.py::marketAssistant`）

1. 模块级创建唯一 `IOPClient(logger=my_log)`（注入在线进程文件日志，不注入则 IOP 失败全静默）；FastAPI `lifespan` 的 startup / shutdown 分别 `await iop_client.start()` / `await iop_client.aclose()`。`start()` 会尽力预热 token 并启动后台续期任务，`aclose()` 取消该任务；预热超时不阻塞服务启动。startup 还先调 `call_stat.startup()` 建表并恢复当天已落库基数（打 INFO 日志），shutdown 在 `finally` 里调 `call_stat.flush()` 补齐不足阈值的尾部增量（`uvicorn` 的 SIGTERM 退出路径不经过 `atexit`）。
2. 请求内创建 `IopLabelProvider`（函数局部变量）：标签属于单个手机号，同请求最多查 1 次 IOP，任何失败降级 `{}`，不跨请求共享。
3. 循环外读两次 `dimensions.db`（各 1 次 `mode=ro`、无进程级缓存）：`load_selling_points(全部 productId)` 批量 `IN` 查 `selling_points` 得 `{product_id: SellingPointRow}`；`load_label_name_mapping()` 全表读 `label_name_mapping` 得 `{label_id: 营销助手显示名字}`（由「标签转换维表.xlsx」写出）。
4. 逐个 `productId`：`services.routing.need_labels(id)` 决定是否取标签（商机回捞、诊断链路与卖点话术都查，控损不查；诊断无静态 ID 白名单，除控损外均需标签），再调 `services.build_product_object(id, telnum, custom_label_info=..., selling_points=..., label_name_mapping=...)` 完成路由、取数与对象构造。提供者保证整请求最多查 1 次 IOP。
5. 按对象类型组装 `allScripts`；业务库未就绪时商机 / 控损仍按空记录出话术，诊断返回 `None` 后由平级的卖点话术模块接手。

只有商机对象持有 `AuxiliaryPortrait` 组件；控损、诊断、兜底不拼画像。诊断链路用请求级 IOP 标签把三类话术里的 `#标签名&` 换成 `ITEMVALUE`；卖点话术不持有画像组件，但会用维表「标签ID」子集 + IOP 返回 + `label_name_mapping` 算出上一版格式的【辅助画像】字符串，并据此选标签专属话术。诊断做话术原文的占位符替换，卖点话术不做。

## 7. 五类路由（四类业务 + 兜底，顺序不可调换）

`services/routing.py::build_product_object()` 是唯一对象工厂。

| 顺序 | 条件（`product_id`） | 数据源 | 业务对象 |
|---|---|---|---|
| 1 | `202608041615335173` / `172` / `174`（商机回捞，映射 `F09554/F09555/F09556`） | `reader.query(avl, telnum)` 单条 | `ProductInfoByOpportunityRecall` |
| 2 | `202609180955095402`（控损，映射 `F09591`） | `reader.query(avl, telnum)` 单条，命中即返回 | `ProductInfoByLossControl` |
| 3 | 诊断（由 `F09629` 动态命中，无静态 product_id 映射） | `diagnosis.build_diagnosis_record()`：8 表（7 表关联 + `F09635` 标签反查），任一环节为空返回 `None` | `ProductInfoByDiagnosis` |
| 4 | 诊断返回 `None`（或前面均未命中） | `selling_point_script.build_selling_point_object()`：只消费 `selling_points` 一行 | `ProductInfoBySellingPoint` |
| 5 | 其余全部 | — | `ProductInfoRaw`（`code: "2"`） |

- `product_id → AVL 前缀` 的两张静态表集中在 `services/routing.py`；`reader.py` 只认 AVL 前缀，不知道产品 ID。
- 商机 / 控损命中即返回；`reader.query` 返回 `{}` 时按空记录出话术，不落到诊断 / 卖点话术 / 兜底。
- 诊断的 `F09629`（`DIM_MARKET_PRODUCT_BUSINESS_ONCE`）本身即「产品ID → 策略 / 产品」映射表；诊断只认自己的 8 张表，任一查询空、场景交集为空或六项输出全空时返回 `None`，由路由继续问平级的卖点话术模块，诊断自己不拼维表话术。卖点话术只消费维表一行，**维表中无该 `product_id`，或有该行但 `product_name`（产品名称）为空时**工厂返回 `None`，才落第 5 类（`code: "2"`）。边界实现见 `services/selling_point_script/logic.py::extract_selling_point_fields()`。
- `selling_points` 不参与路由判定，只作为数据透传给对象工厂；诊断分支只收 `custom_label_info`，卖点话术分支收 `selling_points` + `custom_label_info` + `label_name_mapping`。

## 8. 业务话术规则

| 业务 | 话术结构 | 关键规则 |
|---|---|---|
| 商机回捞 | 四段【通话总结 / 用户需求 / 已推荐政策 / 拒绝原因】HTML | 记录缺失 / 空值填「无」；末尾按需追加【辅助画像】 |
| 控损 | `strategy_name` 自定义标签转 HTML | 无记录输出「无历史话术记录」 |
| 诊断 | 六段【资费名称 / 资费摘要 / 痛点诊断 / 切入话术 / 营销话术 / 挽留话术】HTML | 空值填「无」；资费名称 / 摘要取 `F09629.BUSINESS_NAME` 按【】内外拆分（同时支持 ASCII `[]`）；只认 8 张表；**【挽留话术】只从 F09632 召回的「拒绝原因 + 挽留话术」里随机输出一条**（按配对整条抽，不拆开），痛点诊断输出全部召回的 REASON 并按首次出现去重（上游同键重复行的兜底），营销话术仍输出全部召回 |
| 卖点维表话术 | 四段【辅助画像 / 切入话术 / 卖点 / 挽留话术】，空段整段不出现 | 与诊断平级的独立业务，不读诊断 8 表；**输出格式逐字符复现上一版 `ProductInfoRewrite` 的 HTML 形状，唯一差异是出口把全部换行转成 `<br>`**：段间 / 段内真实换行在 `build_output_script()` 末尾统一转 `<br>`（与商机 / 控损一致，最终不含 `\n`）、`【xxx】` 包 `<strong>`、切入 / 挽留前缀蓝色 span、卖点首段标红 + 标题加粗。db 列 → 段：「产品名称」→ 响应 `productName`；「生成话术」→【卖点】；「兜底话术」→【切入话术】/【挽留话术】；「匹配结果2」为空时仅当「兜底话术」为空且「标签0」非空才回退输出兜底组，否则不输出切入 / 挽留；「标签ID」+ IOP 返回 + `label_name_mapping` →【辅助画像】，IOP 命中「匹配结果2」里的 ID 时改用对应「标签N」话术；不做 `#标签名&` 字段替换 |

- 诊断标签替换：切入 / 营销 / 挽留三段话术里的 `#标签名&`（如 `#客运个性化运营标签34&`）统一替换——按标签名查第 8 张表 `F09635`（主键 `tag_name`）得 `tag_id`，它即 IOP 返回的 `ITEMCODE`，用该 ITEMCODE 对应的 `ITEMVALUE` 替换整个占位符；**只作用于诊断成功 record**。卖点话术模块的原文不存在待替换字段，不收集、不替换、逐字符输出。标签名未登记、`ITEMCODE` 不存在、`ITEMVALUE` 为 `None` 或 IOP 降级 `{}` 时诊断里的占位符原样保留。挽留段的随机选取只发生在输出拼装阶段：`retain_pairs` 仍是全量召回，标签替换对全集生效，被选中的那条一定已完成替换。
- 辅助画像：商机侧 `build_auxiliary_portrait()` 按 `标签名: <strong><span style="color:blue;">值</span></strong>; ` 逐项拼接，跳过空值，末尾加 `【辅助画像】` 前缀并 `rstrip`；卖点话术侧 `selling_point_script.build_auxiliary_portrait()` 额外套用上一版硬编码转换（显示名映射 / `0.00→无` / 加元 / 加 M / 是否 / 降档阈值标红 / 同名去重），HTML 形状一致。空输入返回空串，不追加该段。
- 标签 ID 子集来自卖点维表 `selling_points.label_id`（换行 / 竖线 / 逗号 / 分号及全角变体拆分、去空、保序去重），IOP 返回决定名称与值；维表决定「查哪些 ID」，IOP 决定「叫什么、值是多少」，`label_name_mapping` 表（由「标签转换维表.xlsx」写出）把 IOP 的 `ITEMNAME` 换成营销助手显示名。
- `productName`：诊断取 `record['business_name']` 原文（即 `F09629.BUSINESS_NAME`）；卖点话术取维表「产品名称」原文（不拆资费名称 / 摘要，也不进 `output_script`）。
- 不复刻离线解析，直接把 `dimensions.db` 转好的 `TEXT` 原文做格式转换。
- 库里所有话术 / 标签列只存原始文本，拆分与解析都在在线读取侧完成，不写回解析结果。

### 实现级规格索引（重建逐字符行为时按此取）

| 规格 | 落点 |
|---|---|
| 四类业务对象的 HTML 逐字模板、控损自定义标签解析栈、辅助画像拼接 | [`services/README.md` 第 9 节](services/README.md) |
| 商机回捞 12 列 / 控损 5 列的完整列名、类型与解析消费字段 | [`sftp_avl2db/README.md` 第 7 节](sftp_avl2db/README.md) |
| `sftp_avl2db/.env` 68 个键的完整键名与当前取值 | [`sftp_avl2db/README.md` 第 5 节](sftp_avl2db/README.md) |
| 诊断常量、正则、排序键、7 表关联精确调用、诊断不适用条件、卖点话术的 db 字段映射与上一版输出格式逐字符规格 | [`services/README.md` 第 6 节](services/README.md) |

## 9. 降级总表

| 触发 | 行为 |
|---|---|
| 业务库缺失 / AVL 未注册 / 查询异常 | `reader` 返回 `{}` / `[]`；商机 / 控损按空记录出话术（仍 `code: "0"`），诊断返回 `None` → 卖点话术模块，API 不 500 |
| 分表 AVL 的 `where` 缺标量 `product_no` | 定位不到分表，返回 `[]`，不广播、不抛异常 |
| 诊断 7 表关联任一为空 / 场景交集为空 / 六项输出全空 | 诊断返回 `None`；路由改问平级的卖点话术模块，按上一版格式输出（辅助画像 / 切入 / 卖点 / 挽留），`code: "0"` |
| 维表中无该 `product_id` 或产品名称为空 | 卖点话术工厂返回 `None`，落 `ProductInfoRaw`（`code: "2"`） |
| 卖点话术原文为空 /「匹配结果2」为空 | 「匹配结果2」为空时仅当「兜底话术」为空且「标签0」非空才回退「标签0」原文输出兜底组，其余情况切入 / 挽留段整段不出现、只留【卖点】；不填「无」，不回退其它「标签N」 |
| 卖点维表库缺失 / 列漂移 / 查询异常 | `dimensions` 降级 `{}`，商机辅助画像为空，卖点话术无行可用 → `code: "2"`，其余话术段照常；`label_name_mapping` 表缺失同样降级 `{}`，辅助画像直接用 IOP 原名 |
| IOP token / 标签超时、连接失败、响应异常、冷却期 | 重试退避后降级 `{}`，不阻塞其它并发请求；商机画像为空，诊断话术里的 `#标签名&` 原样保留；**卖点话术同样受影响**：无【辅助画像】段，且切入 / 挽留话术回落「兜底话术」组。降级原因（status）记进 `log/`，标签端点明确返回 token 失效时自动 invalidate + 重刷 + 重试一次，不再带病跑完整个有效期 |
| 调用量库写入 / 启动读取失败 | 保留 `_PENDING` 下轮重试，或基数按 0 恢复，服务照常启动 |
| 维表 xlsx 缺失 / 缺必需表头列 | 写入侧告警跳过本表，保留上次成功版本，下轮重试 |

细目见 [`services/README.md`](services/README.md)、[`IOP/README.md`](IOP/README.md)、[`call_stat/Readme.md`](call_stat/Readme.md)、[`local_excel2db/README.md`](local_excel2db/README.md)。

## 10. 部署与运行

### 10.1 安装

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements.txt
```

平台要求见第 1 节（仅 Linux / Unix）。

容器化部署与上面的 venv 路径等价，任选其一：镜像基线为 `python:3.12-slim`，`Dockerfile` 设 `TZ=Asia/Shanghai`、`EXPOSE 8989`，默认 `CMD ["api"]`。

```bash
docker build -t aiassistantcrm:v5.0 .
docker run -d --name crm -p 8989:8989 aiassistantcrm:v5.0
docker run -d --name crm -p 8989:8989 aiassistantcrm:v5.0 full
```

`docker-entrypoint.sh` 作为 PID 1，按首参分派：`api`（默认，只 `exec python3 main.py`）、`full`（12 个 `python3 -m sftp_avl2db --task <AVL>` + 1 个 `python3 -m local_excel2db` + `python3 main.py`，共 14 个进程，AVL 清单与第 10.5 节一致）、`shell`（`exec /bin/sh`）、其它原样 `exec`。`CRM_PORT` 环境变量由该脚本写回 `/app/.env`（`SKIP_ENV_WRITE=1` 禁用写回）；`app_config.load_config()` 仍只读 `.env`，不读系统环境变量。整目录挂载时脚本校验 `app_config.py` 与 `local_excel2db/registry.json` 存在，`local_excel2db/runtime/sqlite_db/dimensions.db` 缺失时自动执行 `python3 -m local_excel2db --once`。镜像构建时 `Dockerfile` 会用各包 `.env.example` 初始化缺失的 `.env`，凭据替换仍按 10.2 节执行。`full` 模式下 API 是主进程，脚本 `wait "$api_pid"` 等它退出即容器退出；`TERM` / `INT` 由 `trap terminate` 转发给 12 个写入进程与维表进程后收尾。

### 10.2 凭据替换（首次部署必做）

仓库随附的项目根、`IOP/`、`services/`、`call_stat/` 四对 `.env` 与 `.env.example` 逐键同值；`sftp_avl2db/` 一对只差键集合：`.env` 比 `.env.example` 多 8 个可选的 `<AVL>_DROP_BEFORE_INSERT` 覆盖键（诊断 8 张表均为 `1`），68 个必需键逐键同值（其中 `F09591_BATCH_SIZE` 与 `F09633_BATCH_SIZE` 均为 `100000`）。`IOP/config.py::_SPECS` 里还内置了一份实际生效的 token 端点地址与凭据。因此直接 `cp .env.example .env` 会原样带入仓库里的明文串，必须逐项替换：

| 文件 | 要替换的键 |
|---|---|
| `sftp_avl2db/.env` 与 `.env.example` | `SFTP_HOST` / `SFTP_PORT` / `SFTP_USERNAME` / `SFTP_PASSWORD` / `SFTP_REMOTE_PATH` |
| `IOP/.env` 与 `.env.example` | `IOP_TOKEN_HOST` / `IOP_TOKEN_PORT` / `IOP_TOKEN_ID` / `IOP_TOKEN_SECRET` / `IOP_CUSTINFO_HOST` / `IOP_CUSTINFO_PORT` |
| `IOP/config.py::_SPECS` | 上表所列 6 个键对应的内置默认值；`.env` 缺文件 / 缺键 / 空值 / 写成 `<...>` 占位符时回落这些内置值 |

最后一处不能漏：`.env` 缺键、空值或写成 `<...>` 占位符时会**回落到 `_SPECS` 的内置值**（见 `IOP/README.md` 第 4 节），只改 `.env` 并不能保证生效值已换掉。生产环境不要把 `.env` 再提交进版本库。改完配置重启对应进程生效（配置对象首次加载后冻结）。

项目根 `.env` 的 `CRM_PORT` 只控制在线服务监听端口，不含任何凭据，首次部署按需改端口即可，不参与上表的凭据替换。

**外部依赖获取口径（属外部依赖）**：上表所列 `SFTP_HOST` / `SFTP_PORT` / `SFTP_USERNAME` / `SFTP_PASSWORD` / `SFTP_REMOTE_PATH` 与 `IOP_TOKEN_HOST` / `IOP_TOKEN_PORT` / `IOP_TOKEN_ID` / `IOP_TOKEN_SECRET` / `IOP_CUSTINFO_HOST` / `IOP_CUSTINFO_PORT` 的获取渠道：<待填写：上游接口人或运维交付渠道>；责任人：<待填写：配置管理员>；缺失行为：SFTP 侧缺任一键启动时 fail-fast，IOP 侧缺文件 / 缺键 / 空值回落 `IOP/config.py::_SPECS` 内置值。

### 10.3 首次上线顺序

1. **先建维表库**：`python -m local_excel2db --once`。输入是仓库根 `excels/` 已预置的两张 xlsx，确认产出 `local_excel2db/runtime/sqlite_db/dimensions.db` 且 `selling_points` 有行。
2. **再灌业务库**：12 个 AVL 各跑一次带 `--date` 的单次补跑（按上游实际文件日期逐个补），产出 `sftp_avl2db/runtime/sqlite_db/{project}/{AVL}.db` 与库内 `_avl_meta` 批次水位。改过分表数（`F09591` / `F09633`）必须重灌对应 AVL 全量。 上游 AVL 数据文件（属外部依赖）：获取渠道 <待填写：上游投递目录 / 接口人>，责任人 <待填写：数据提供方>，真实文件日期 <待填写：YYYYMMDD>；未取得文件时不臆造日期，常驻进程会在文件到达后自动拉取。
3. **最后起常驻进程**：`python main.py`，加上 §10.5 代码块里的 12 个 `--task` 常驻进程与 `python -m local_excel2db`（共 14 个，见第 1 节）。
4. 业务库尚未就绪也允许先起在线服务：商机 / 控损按空记录出话术（仍 `code: "0"`），诊断返回 `None` 后由平级的卖点话术模块接手，API 不会 500。

### 10.4 验收（重建成功的判定）

```bash
python -m call_stat --self-check              # SELF-CHECK OK
python tests/check_diagnosis_offline.py      # ALL PASS（成立条件见第 11 节）
python tests/check_purge_rotation.py         # ALL PASS
python tests/check_avl_freshness.py          # ALL PASS
python tests/check_drop_before_insert.py     # ALL PASS
# 起服务后：
python tests/check_param_validation.py
python tests/check_call_stat.py
python tests/check_online_api.py
```

起服务后的三个冒烟脚本只打印响应、不做自动断言：判定口径按本节「期望响应基线」逐条人工比对（非法入参恒 `res_code=1`；主链路与统计查询恒 `res_code=0`；`code` 按对象类型判定）。

期望响应基线（业务库为空时）：商机 3 个 ID 返回四段各填「无」的 HTML（`code: "0"`）；控损 `202609180955095402` 返回 `script: "无历史话术记录"`（`code: "0"`）；诊断返回 `None` 后由卖点话术模块接手——维表有该行且产品名称非空时返回与独立参照逐字符一致的 script（`code: "0"`），维表无该行或产品名称为空才 `code: "2"`；`res_code` 恒为 `0`，非法入参恒返回 HTTP 200 + `res_code: 1`。在线冒烟脚本只打印结果、结论靠人看，不是自动断言。

### 10.5 常驻进程与维护命令

一键脚本（放在项目根，装完依赖后执行）：

```bash
python -m pip install -r requirements.txt
bash start_project.sh                 # 启动 14 个常驻进程；若 dimensions.db 不存在会先 --once 建库
bash start_project.sh status         # 查看各进程
bash start_project.sh stop           # 优雅停止（先 SIGTERM 让在线进程 flush 调用量）
DRY_RUN=1 bash start_project.sh      # 只打印不执行
# 可选：SKIP_SFTP=1（不启 12 个写入进程）、BOOTSTRAP_AVL_DATE=YYYYMMDD（启动前按日期补跑一次）
#       PYTHON_BIN=/path/to/python（覆盖解释器，默认 .venv/bin/python 再 python3）
#       INSTALL_DEPS=1（依赖缺失时自动 pip install -r requirements.txt）
#       SKIP_DEP_CHECK=1（跳过依赖检查）、SKIP_BOOTSTRAP_DB=1（dimensions.db 缺失也不自动 --once 建库）
```

PID 与进程 stdout 在 `log/startup/`，各写入侧自身日志仍在各自 `runtime/log/`。等价的手工启动方式：

```bash
# 写入侧：12 个 AVL 各起一个常驻进程
python -m sftp_avl2db --task F09554
python -m sftp_avl2db --task F09555
# ... F09556 / F09591 / F09627 / F09628 / F09629 / F09630 / F09631 / F09632 / F09633 / F09635

python -m local_excel2db          # 卖点维表写入（默认 600s 轮询）
python main.py                    # 在线 API :8989（端口见项目根 .env 的 CRM_PORT）
```

常用维护命令：

```bash
python -m sftp_avl2db --task F09591 --date 20261110              # 单次补跑
python -m sftp_avl2db --task F09591 --date 20261110 --force-reload
python -m local_excel2db --once                                   # 只导一轮
python -m local_excel2db --once --force                          # 忽略指纹强制重导
python -m call_stat --date 2026-11-16                            # 查单日调用量
python -m call_stat --month 2026-11 --export                     # 导出 CSV
python -m call_stat --flush                                      # 落盘未达阈值增量
```

离线复跑 / 单测可注入 `--local-dir`、`--runtime-dir`、`--max-rounds`，产出隔离到临时目录；离线复跑仍会装配 `TaskLogger`，需装好 `loguru`（编程式调用 `cli.main(logger=...)` 或 `build_task(logger=InMemoryLogger())` 可注入替身）。部署时可把各包 `runtime/` 软链到独立数据盘，不要整目录覆盖包目录。改表名、列映射、主键或标签列数后须删除 `dimensions.db` 重建，并同步在线侧 `SELLING_POINTS_*` 声明。

## 11. 自检

```bash
python -m call_stat --self-check                # 统计模块自检（临时库用完即删）
python tests/check_diagnosis_offline.py        # 诊断端到端离线自检；缺在线依赖时第 7 段 SKIP，末尾仍打印 ALL PASS
python tests/check_legacy_output_parity.py    # 真实 dimensions.db 全量逐字符对照上一版；库不存在时 SKIP
python tests/check_purge_rotation.py           # 库表清理轮转自检，结论 ALL PASS
python tests/check_online_api.py               # 在线主链路冒烟（需先起服务）
python tests/check_param_validation.py         # 入参校验冒烟
python tests/check_call_stat.py                # 调用量 GET 冒烟
python tests/check_iop_direct.py 13900000000   # IOP 上游直连冒烟（打印降级 status）
python tests/check_iop_token_raw.py            # 抓 token 端点原始响应，核对 expires_in 字段/单位
python tests/check_iop_offline.py              # IOP 修复离线自检（不打真实上级）
```

`check_diagnosis_offline.py` 真正断言的是：`F09633` 建满 128 张分表、号码不串表、按 `telnum` 精确定位且缺 `product_no` 时降级 `[]`；写入侧 / 在线侧分表口径一致；维表「标签ID」拆分与库缺失降级；商机画像端到端来自维表 → IOP；诊断 7 表关联与六段话术、诊断不适用返回 `None`；平级卖点话术模块（**把上一版 `ProductInfoRewrite.output_script` 的逐字符期望固化成 `EXPECTED_LEGACY_OUTPUT`**，离线与在线两处断言相等；另用 `tests/legacy_output_reference.py` 直译上一版四个历史文件，对「有 IOP 返回 + `label_name_mapping` + 标签专属话术命中」的完整输出做 golden 逐字符比对；`tests/check_legacy_output_parity.py` 再用真实 `dimensions.db` 全量（当前 1361 个 product_id × 多组 IOP 返回 = 5121 次输出）做独立 oracle 对照，含辅助画像 896 次、不一致 0；切入 / 卖点 / 挽留的蓝 / 红 span 与加粗标题、段间 `\n`、「匹配结果2」为空不输出切入挽留、按标签选话术可选路径、降级边界与 `#标签名&` 原样保留）；业务库未就绪时商机 / 控损按空记录出话术；五类路由输出与 `code` 按对象类型判定；商机回捞与诊断链路共用请求级 IOP 标签（整请求最多 1 次）、`F09635` 按 `tag_name` 反查并把诊断三类话术里的 `#标签名&` 换成 `ITEMVALUE`（含降级保留）。

以下三条**不在任何自检脚本的断言里**，只由代码结构保证、需人工或另行验证，不要把它们算进 `ALL PASS`：`app.router.on_startup` 为空（lifespan 注册）、每请求只读 2 次 `dimensions.db`（`selling_points` + `label_name_mapping`）、请求间标签 / 画像隔离。调用量阈值落库、重启恢复、shutdown `flush()` 不丢尾部增量则由 `python -m call_stat --self-check` 独立断言，输出 `SELF-CHECK OK`。

`ALL PASS` 有成立条件：`check_diagnosis_offline.py` 的第 1～6 段只用标准库 + 临时 SQLite，缺 `fastapi / pydantic / loguru / aiohttp / uvicorn` 任一都不会报错；第 7 段「在线路由」会打印全角括号的 `[SKIP] 未加载在线侧（{异常类型}: {异常信息}）`（异常类型随实际缺失的依赖变化，不只是 `uvicorn`），脚本末尾**仍然输出 `ALL PASS`**。因此 `ALL PASS` 只代表已执行段的断言通过；`import main` 与第 7 段的在线侧五类路由断言，必须先装齐在线依赖才会真正跑起来。`tests/README.md` 第 4 节也记录了这一跳过条件。

## 12. 文档索引

- [`requirements.txt`](requirements.txt)：运行依赖、版本下限与安装命令
- [`services/README.md`](services/README.md)：在线业务层分层、五类路由、读取配置、诊断 8 表取数与平级卖点话术模块（输出格式复现上一版）
- [`IOP/README.md`](IOP/README.md)：标签客户端的入口、请求链路、上游契约、配置、token 失效自愈与并发约束，以及「同样查询一次无辅助画像」的排查步骤
- [`sftp_avl2db/README.md`](sftp_avl2db/README.md)：AVL 注册、表结构、解析写入、清理轮转
- [`local_excel2db/README.md`](local_excel2db/README.md)：registry 配置、库表结构与导入流程
- [`call_stat/Readme.md`](call_stat/Readme.md)：调用量统计的存储、生命周期、GET 契约与自检
- [`io_samples/README.md`](io_samples/README.md)：样例文件清单与离线复跑方式
- [`tests/README.md`](tests/README.md)：冒烟 / 自检脚本清单与运行方式
