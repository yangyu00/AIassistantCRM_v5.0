# AIassistantCRM v5.0 · 项目说明

本文件是全局规格：系统组成、数据流、对外接口、路由、降级、部署与自检。各组件与业务层的详细契约见文末索引；按这些文档可重建代码。

## 1. 系统组成与运行进程

| 侧 | 包 / 入口 | 进程数 | 职责 |
|---|---|---|---|
| 写入侧 | `python -m sftp_avl2db --task <AVL>` | 12 | 每个 AVL 一个进程：SFTP 拉取 AVL → 解析 → 写 `{AVL}.db` |
| 写入侧 | `python -m local_excel2db` | 1 | 轮询项目根 `excels/*.xlsx` → `dimensions.db` |
| 在线侧 | `python main.py` | 1 | FastAPI 在线 API（`0.0.0.0:8989`），只读业务库与维表，按 `product_id` 拼话术 |
| 在线侧模块 | `call_stat/` | 0（进程内） | HTTP 访问按天计数，缓冲落 `call_stat/runtime/call_sum.db` |

部署时 14 个进程都要起。在线进程唯一写入是 `call_stat/runtime/call_sum.db`；业务库与维表均由写入侧独立维护。各包配置与运行时产物按 `__file__` 锚在包目录内，与启动 CWD 无关。

依赖：Python 3.10+；在线侧 `fastapi / uvicorn / pydantic / aiohttp / loguru`；写入侧 `paramiko`（SFTP）、`openpyxl`（xlsx）、`portalocker`（可选，单实例锁）；冒烟脚本 `requests`。各包 `import` 本身零 I/O、零第三方依赖，第三方库在入口函数内延迟加载。

## 2. 目录结构

| 路径 | 职责 |
|---|---|
| `main.py` | 在线 API：入参校验、IOP 客户端 lifespan、请求级编排、响应组装；顶部常量为服务端口 / 路由 / 日志目录 |
| `src/` | 在线进程日志初始化与自定义异常 |
| `services/` | 在线话术业务层：读取配置、业务库只读、卖点维表只读、四类路由、三类业务话术对象 |
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
                                                          services/dimensions.py ─┘（每请求只读 1 次 dimensions.db）

main.py ──> call_stat/runtime/call_sum.db（在线进程唯一写入）
```

物理布局约定：

- 固定表名 = `{AVL}`，分表名 = `{AVL}_{分表号}`，分表号 `zlib.crc32(str(product_no)) % NUM_TABLES`。
- `F09591`（控损）与 `F09633`（诊断·用户场景对应关系）按 `product_no` 分表，默认 128 张；`F09633` 是千万级表，分表键与在线查询键一致，一次请求只命中一张分表。
- 写入侧与在线侧各自独立声明库路径、表字段与分表数，代码 / 配置互不引用；唯一交集是磁盘上的 db 文件。改列顺序、分表数或库路径必须同步两侧声明（写入侧 `sftp_avl2db/table_schemas.py`、`avl_specs.py`、`.env`；在线侧 `services/db_config.py`、`services/.env`）。改分表数须重灌对应 AVL 全量数据。
- 商机、控损、诊断的 AVL 归属与库文件见 [`sftp_avl2db/README.md`](sftp_avl2db/README.md) 与 [`services/README.md`](services/README.md)。

## 4. 配置来源

| 作用域 | 唯一来源 | 装配入口 | 缺文件 / 缺键行为 |
|---|---|---|---|
| IOP 客户端 | `IOP/.env`（20 键） | `IOPConfig.load()` | 回落 `config.py` 内置默认值 |
| SFTP 写入 | `sftp_avl2db/.env`（67 键） | `load_profiles()` | 缺任一键直接 fail-fast |
| 调用量统计 | `call_stat/.env`（4 键） | `load_config()` | 回落 `config.py` 内置默认值并告警 |
| 在线读取 | `services/.env`（4 键） | `db_config.load()` | 回落 `db_config.py` 代码内默认值 |
| 维表写入 | `local_excel2db/registry.json` | `load_registry()` | 配置损坏退出码 2 |
| 在线服务 | `main.py` 顶部常量 | — | `LOG_DIR`、`FASTAPI_CONFIG`、`MARKET_ASSISTANT_URL`、`MARKET_ASSISTANT_USERS` |

`.env` 语法只认整行 `#` 注释与 `KEY=VALUE`，不支持引号、行内注释、`${VAR}` 插值。仓库随附的 `.env` 为实际生效字符串，`.env.example` 是同值的带注释键模板；改完配置重启对应进程生效（配置对象首次加载后冻结）。

关键键名：

- `IOP/.env`：`IOP_TOKEN_HOST/PORT/ID/SECRET`、`IOP_CUSTINFO_HOST/PORT/VERSION/TYPE`、`IOP_TIMEOUT_TOTAL/CONNECT/SOCK_READ`、`IOP_RETRY_MAX_ATTEMPTS/BACKOFF_BASE/BACKOFF_FACTOR/BACKOFF_MAX/COOLDOWN`、`IOP_LIFETIME_DEFAULT_EXPIRES_IN/REFRESH_BUFFER/MIN_LIFETIME/MAX_EXPIRES_IN`。
- `services/.env`：`AVL_DB_ROOT`、`DIMENSIONS_DB`、`LOSS_CONTROL_NUM_TABLES`、`F09633_NUM_TABLES`。
- `call_stat/.env`：`CALL_STAT_FLUSH_THRESHOLD`（默认 100）、`CALL_STAT_QUERY_PATH`（默认 `/api/callStat`）、`CALL_STAT_DB_FILENAME`（默认 `call_sum.db`）、`CALL_STAT_EXPORT_DIRNAME`（默认 `export`）。
- `sftp_avl2db/.env`：SFTP 公共 5 项 + 每个 AVL 的 `<AVL>_BATCH_SIZE / _DATA_EXPIRE_DAYS / _LOCAL_FILE_EXPIRE_DAYS / _POLL_INTERVAL_SECONDS / _PURGE_CYCLE_DAYS`，分表 AVL 另加 `<AVL>_NUM_TABLES`；无项目级键。

## 5. 在线 API 契约

### POST `/api/speechQryService`

请求体（`Args` / `ProductIds`，pydantic 模型）：

```json
{"sceneId": "", "telnum": "15020868271", "productId": [{"id": "202608041615335173"}]}
```

- `telnum`：非空、11 位数字；`productId`：非空数组，每项 `id` 非空。
- 违反校验返回 HTTP 200 + `{"res_code": 1, "res_desc": "...", "result": ""}`（`telnumError` / `productIdError`）。

成功响应：

```json
{
  "res_code": 0, "res_desc": "",
  "result": {"allScripts": [
    {"productId": "...", "productName": "...", "script": "...", "code": "0"}
  ]}
}
```

`code` 按产品对象类型判定：`"0"` 为三类业务对象带话术；`"2"` 为兜底对象，`productName` 与 `script` 均为空。不能按 `product_name` 是否为空判定。

### GET `/api/callStat`

- 参数 `date=YYYY-MM-DD` 或 `month=YYYY-MM`，必须且只能提供一个；格式非法返回 `res_code: 1`。
- 单日返回 `{"date": "2026-11-16", "callCount": 123}`；整月返回 `{"month": "2026-11", "days": [{"date": "2026-11-01", "callCount": 0}, ...]}`（补齐整月）。
- 该 GET 自身也计入调用量，但计数在 `finally` 阶段，本次返回值不含本次请求。

## 6. 请求编排（`main.py::marketAssistant`）

1. 模块级创建唯一 `IOPClient`；FastAPI `lifespan` 的 startup / shutdown 分别 `await iop_client.start()` / `await iop_client.aclose()`。
2. 请求内创建 `IopLabelProvider`（函数局部变量）：标签属于单个手机号，同请求最多查 1 次 IOP，任何失败降级 `{}`，不跨请求共享。
3. 循环外调一次 `services.dimensions.load_selling_points(全部 productId)`，批量 `IN` 查 `selling_points`（`mode=ro`，每请求 1 次，无进程级缓存），得到 `{product_id: SellingPointRow}`。
4. 逐个 `productId`：`services.routing.need_labels(id)` 决定是否取标签（仅商机回捞 3 个 ID 查），再调 `services.build_product_object(id, telnum, custom_label_info=..., selling_points=...)` 完成路由、取数与对象构造。
5. 按对象类型组装 `allScripts`；业务库未就绪时商机 / 控损仍按空记录出话术，诊断走卖点维表兜底。

只有商机对象持有 `AuxiliaryPortrait` 组件；控损、诊断、兜底对象不持有画像相关属性，也不查 IOP 标签。

## 7. 四类路由（顺序不可调换）

`services/routing.py::build_product_object()` 是唯一对象工厂。

| 顺序 | 条件（`product_id`） | 数据源 | 业务对象 |
|---|---|---|---|
| 1 | `202608041615335173` / `172` / `174`（商机回捞，映射 `F09554/F09555/F09556`） | `reader.query(avl, telnum)` 单条 | `ProductInfoByOpportunityRecall` |
| 2 | `202609180955095402`（控损，映射 `F09591`） | `reader.query(avl, telnum)` 单条，命中即返回 | `ProductInfoByLossControl` |
| 3 | 诊断（由 `F09629` 动态命中，无静态 product_id 映射） | `diagnosis.build_diagnosis_record()`：7 表关联，任一环节失败走卖点维表兜底 | `ProductInfoByDiagnosis` |
| 4 | 其余全部 | — | `ProductInfoRaw`（`code: "2"`） |

- `product_id → AVL 前缀` 的两张静态表集中在 `services/routing.py`；`reader.py` 只认 AVL 前缀，不知道产品 ID。
- 商机 / 控损命中即返回；`reader.query` 返回 `{}` 时按空记录出话术，不落到诊断 / 兜底。
- 诊断的 `F09629`（`DIM_MARKET_PRODUCT_BUSINESS_ONCE`）本身即「产品ID → 策略 / 产品」映射表；诊断走 7 表关联，任一查询空、场景交集为空或六项输出全空时用卖点维表兜底，仍返回 `ProductInfoByDiagnosis`（`code: "0"`）；连维表也无该 `product_id` 时才落第 4 类。
- `selling_points` 不参与路由判定，只作为数据透传给对象工厂。

## 8. 业务话术规则

| 业务 | 话术结构 | 关键规则 |
|---|---|---|
| 商机回捞 | 四段【通话总结 / 用户需求 / 已推荐政策 / 拒绝原因】HTML | 记录缺失 / 空值填「无」；末尾按需追加【辅助画像】 |
| 控损 | `strategy_name` 自定义标签转 HTML | 无记录输出「无历史话术记录」 |
| 诊断（正常） | 六段【资费名称 / 资费摘要 / 痛点诊断 / 切入话术 / 营销话术 / 挽留话术】HTML | 空值填「无」；资费名称 / 摘要取 `F09629.BUSINESS_NAME` 按【】内外拆分（同时支持 ASCII `[]`） |
| 诊断（维表兜底） | 四段【资费名称 / 资费摘要 / 切入话术 / 挽留话术】 | 省略【痛点诊断】【营销话术】两段；产品名称取维表「产品名称」按【】拆分；话术取「兜底话术」列原文，为空时回退第一个非空「标签N」原文 |

- 辅助画像：`build_auxiliary_portrait()` 按 `标签名: <strong><span style="color:blue;">值</span></strong>; ` 逐项拼接，跳过空值，末尾加 `【辅助画像】` 前缀并 `rstrip`；空输入返回空串，不追加该段。
- 标签 ID 子集来自卖点维表 `selling_points.label_id`（换行 / 竖线 / 逗号 / 分号及全角变体拆分、去空、保序去重），IOP 返回决定名称与值；维表决定「查哪些 ID」，IOP 决定「叫什么、值是多少」。
- `productName`：诊断取 `record['business_name']` 原文（正常链路为 `F09629.BUSINESS_NAME`，兜底为维表「产品名称」）。
- 库里所有话术 / 标签列只存原始文本，拆分与解析都在在线读取侧完成，不写回解析结果。

## 9. 降级总表

| 触发 | 行为 |
|---|---|
| 业务库缺失 / AVL 未注册 / 查询异常 | `reader` 返回 `{}` / `[]`；商机 / 控损按空记录出话术（仍 `code: "0"`），诊断触发维表兜底，API 不 500 |
| 分表 AVL 的 `where` 缺标量 `product_no` | 定位不到分表，返回 `[]`，不广播、不抛异常 |
| 诊断 7 表关联任一为空 / 场景交集为空 / 六项输出全空 | 走卖点维表兜底，输出四段，`code: "0"` |
| 维表中无该 `product_id` 或产品名称为空 | 兜底失败，落 `code: "2"` |
| 兜底话术原文为空 | 回退第一个非空「标签N」原文；全空时切入 / 挽留段填「无」 |
| 卖点维表库缺失 / 列漂移 / 查询异常 | `dimensions` 降级 `{}`，商机辅助画像为空，其余话术段照常 |
| IOP token / 标签超时、连接失败、响应异常、冷却期 | 重试退避后降级 `{}`，不阻塞其它并发请求 |
| 调用量库写入 / 启动读取失败 | 保留 `_PENDING` 下轮重试，或基数按 0 恢复，服务照常启动 |
| 维表 xlsx 缺失 / 缺必需表头列 | 写入侧告警跳过本表，保留上次成功版本，下轮重试 |

细目见 [`services/README.md`](services/README.md)、[`IOP/README.md`](IOP/README.md)、[`call_stat/Readme.md`](call_stat/Readme.md)、[`local_excel2db/README.md`](local_excel2db/README.md)。

## 10. 部署与运行

```bash
# 写入侧：12 个 AVL 各起一个常驻进程
python -m sftp_avl2db --task F09554
python -m sftp_avl2db --task F09555
# ... F09556 / F09591 / F09627 / F09628 / F09629 / F09630 / F09631 / F09632 / F09633 / F09635

python -m local_excel2db          # 卖点维表写入（默认 600s 轮询）
python main.py                    # 在线 API :8989
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

离线复跑 / 单测可注入 `--local-dir`、`--runtime-dir`、`--max-rounds`，产出隔离到临时目录。部署时可把各包 `runtime/` 软链到独立数据盘，不要整目录覆盖包目录。改表名、列映射、主键或标签列数后须删除 `dimensions.db` 重建，并同步在线侧 `SELLING_POINTS_*` 声明。

## 11. 自检

```bash
python -m call_stat --self-check                # 统计模块自检（临时库用完即删）
python tests/check_diagnosis_offline.py        # 诊断端到端离线自检，结论 ALL PASS
python tests/check_purge_rotation.py           # 库表清理轮转自检，结论 ALL PASS
python tests/check_online_api.py               # 在线主链路冒烟（需先起服务）
python tests/check_param_validation.py         # 入参校验冒烟
python tests/check_call_stat.py                # 调用量 GET 冒烟
python tests/check_iop_direct.py 13900000000   # IOP 上游直连冒烟
```

核心断言：`import main` 正常、`app.router.on_startup` 为空（走 lifespan）；四类路由输出符合第 7 节；`code` 按对象类型判定；`F09633` 128 张分表按 `telnum` 精确定位且不广播；维表「标签ID」拆分与商机画像来自维表；诊断兜底输出四段且省略两段；每请求只读一次维表、只有商机触发 IOP 查询；请求间标签 / 画像隔离；调用量阈值落库、重启继续计数、shutdown `flush()` 不丢尾部增量。

## 12. 文档索引

- [`services/README.md`](services/README.md)：在线业务层分层、路由、读取配置、诊断取数与兜底
- [`IOP/README.md`](IOP/README.md)：标签客户端的入口、请求链路、上游契约、配置与并发约束
- [`sftp_avl2db/README.md`](sftp_avl2db/README.md)：AVL 注册、表结构、解析写入、清理轮转
- [`local_excel2db/README.md`](local_excel2db/README.md)：registry 配置、库表结构与导入流程
- [`call_stat/Readme.md`](call_stat/Readme.md)：调用量统计的存储、生命周期、GET 契约与自检
- [`io_samples/README.md`](io_samples/README.md)：样例文件清单与离线复跑方式
- [`tests/README.md`](tests/README.md)：冒烟 / 自检脚本清单与运行方式
