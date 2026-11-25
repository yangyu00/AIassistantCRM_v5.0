# sftp_avl2db · SFTP AVL → SQLite 业务库

每个 AVL 对应一个 db 的纯写入侧代码包，只负责「SFTP 拉取 AVL → 解析 → 写进 SQLite」，不提供在线查询入口，也不声明在线侧的库路径、表字段或查询条件。在线业务侧读哪些库、按什么键查由 `services/db_config.py` 独立维护；两侧唯一交集是磁盘上的 db 文件。

配置与运行时产出都锚在包内、按 `__file__` 定位，与启动 CWD 无关：`.env` 是本包唯一配置来源，`runtime/` 是唯一产出目录。一个 AVL = 一个 `{AVL}.db`，`project`（项目名）负责把同一业务的本地 AVL 与 db 收进同一对文件夹；表结构按名字放在 `table_schemas.py` 里，多个 AVL 可复用。库内固定表叫 `{AVL}`，分表叫 `{AVL}_{分表号}`。

`import sftp_avl2db` 零 I/O、零第三方依赖；组件按「配置 / 解析 / 写入 / I-O / 编排」五层拆分，每层可单独 import、替换、测试。

## 1. 调用链地图

```
python -m sftp_avl2db --task F09554
  └─ cli.main()                 解析参数、校验日期、装配、返回退出码
     └─ profiles.build_task()  按 profile 拼出 AssembledTask（全部依赖可注入）
        └─ runner.serve_forever()            拿锁 → 单次/常驻 → 清理 → 释放锁
           ├─ runner.run_once_for_date()     建表、连源、遍历该 AVL 的前缀
           │  └─ runner.import_one()        清单 → stat → fetch → parse → write → 登记
           │     ├─ config.remote_name_for()   拼远程文件名（唯一来源）
           │     ├─ parsers.*.parse()          生成器产出逻辑记录
           │     ├─ sink.SqliteSink.write()    按 layout 分桶、满批 commit
           │     └─ state.FetchedManifest.add() 成功后才登记
           └─ runner.cleanup_expired()      本地文件(每轮) + 库表过期数据(按周期每天一次)
```

## 2. 五层与文件

| 层 | 模块 | 顶层依赖 |
|---|---|---|
| 配置 | `config.py`、`avl_specs.py`、`table_schemas.py` | 无 |
| 解析 | `parsers/` | 无 |
| 写入 | `sink/` | 仅标准库 `sqlite3` |
| I/O | `remote.py`、`state.py`、`task_logging.py` | 无（`paramiko` / `loguru` / 锁库延迟导入；`loguru` 是 `TaskLogger` 的**运行时必需**依赖，未装且未注入 logger 时抛 `RuntimeError`，可注入 `InMemoryLogger()` 替身） |
| 编排 | `runner.py`、`profiles.py`、`cli.py` | 无 |

公共 API 只认包根 `sftp_avl2db`：`__all__` 列出契约与工厂，I/O 类通过模块级 `__getattr__` 延迟导出。`protocols.py` 是唯一契约来源（Protocol 与结果数据类），照协议写替身即可替换任一层。

## 3. AVL 与运行

`project` 只用于目录分组；轮询、清理、批次、分表数等运行参数一律按 AVL 粒度声明为 `<AVL>_*`。

| 项目 project（仅目录） | AVL / `--task` | schema | 库文件 | 表 |
|---|---|---|---|---|
| `opportunity_recall` | `F09554` | `opportunity_recall` | `sqlite_db/opportunity_recall/F09554.db` | `F09554` |
| `opportunity_recall` | `F09555` | `opportunity_recall` | `sqlite_db/opportunity_recall/F09555.db` | `F09555` |
| `opportunity_recall` | `F09556` | `opportunity_recall` | `sqlite_db/opportunity_recall/F09556.db` | `F09556` |
| `loss_control` | `F09591` | `loss_control` | `sqlite_db/loss_control/F09591.db` | `F09591_0 … 127` |
| `diagnosis` | `F09627` | `scene_rank` | `sqlite_db/diagnosis/F09627.db` | `F09627` |
| `diagnosis` | `F09628` | `product_scene` | `sqlite_db/diagnosis/F09628.db` | `F09628` |
| `diagnosis` | `F09629` | `product_business` | `sqlite_db/diagnosis/F09629.db` | `F09629` |
| `diagnosis` | `F09630` | `scene_qrhuashu` | `sqlite_db/diagnosis/F09630.db` | `F09630` |
| `diagnosis` | `F09631` | `product_yxhuashu` | `sqlite_db/diagnosis/F09631.db` | `F09631` |
| `diagnosis` | `F09632` | `product_wlhuashu` | `sqlite_db/diagnosis/F09632.db` | `F09632` |
| `diagnosis` | `F09633` | `conf_user_scene` | `sqlite_db/diagnosis/F09633.db` | `F09633_0 … 127` |
| `diagnosis` | `F09635` | `tagid_tagname` | `sqlite_db/diagnosis/F09635.db` | `F09635` |

```bash
python -m sftp_avl2db --task F09554
python -m sftp_avl2db --task F09591 --date 20261110
python -m sftp_avl2db --task F09591 --date 20261110 --force-reload
```

- `--date YYYYMMDD` 为单次模式（拉完指定**文件名日期**后退出），不传则常驻轮询，每轮兜底检查「昨天 + 今天」。
- `--force-reload` 连清单命中也强制重下重入库。
- 生产每个 AVL 各起一个常驻进程，靠 `runtime/state/{AVL}.lock` 保证单机单实例。
- 注入型选项（离线复跑 / 单测）：`--local-dir DIR` 把本地目录当远程、不连 SFTP；`--runtime-dir DIR` 把产出隔离到临时目录；`--max-rounds N` 限制常驻轮数。

远程文件为 `{AVL}{YYYYMMDD}000000.AVL`，制表符分隔、UTF-8；本地落在 `runtime/files/{project}/`，日志 `runtime/log/{AVL}_INFO|ERROR_日期.log` 按天轮转、保留 7 天。

## 4. 目录布局

```
runtime/
  files/{project}/{AVL}{日期}000000.AVL      ← 输入
  sqlite_db/{project}/{AVL}.db              ← 输出
  state/{AVL}.fetched  {AVL}.lock
  log/{AVL}_INFO|ERROR_日期.log
```

同一项目的多个 AVL 共用 `files/{project}/` 与 `sqlite_db/{project}/`，库文件名就是 AVL 前缀；不同项目天然分目录。

## 5. 配置

唯一来源 `sftp_avl2db/.env`（68 个键），唯一入口 `load_profiles()`；不读系统环境变量，没有第二套解析入口。语法只认整行 `#` 注释与 `KEY=VALUE`，不做引号、行内注释或 `${VAR}` 插值。

| 文件 | 作用 |
|---|---|
| `.env` | 与 `config.py` 同目录，日常改这个；`repr` 自动屏蔽 `SFTP_PASSWORD` |
| `.env.example` | 带注释的配置说明，与 `.env` 同值；新环境可 `cp .env.example .env` |
| `config.py::_AVLS` | `(AVL 名, 项目名)` 声明表；AVL 名同时是文件前缀、db 文件名与 `.env` 键前缀 |
| `avl_specs.py` | 每个 AVL 的静态声明：项目、schema 名、解析器、是否分表；`table_name()` 表名规则、`shard_index()` 分表口径、`validate_specs()` 启动校验 |
| `table_schemas.py` | `TABLE_SCHEMAS`（列定义）与 `TABLE_PRIMARY_KEYS`（主键），供建表 DDL、幂等与解析器列数校验 |

`.env` 分组：SFTP 公共 5 项（`SFTP_HOST/PORT/USERNAME/PASSWORD/REMOTE_PATH`）+ 全量替换模式开关 1 项（`AVL_DROP_BEFORE_INSERT`）+ 12 个 AVL 各 5 个运行参数（60 项）+ 2 个分表 AVL 各 1 个 `NUM_TABLES` = 68 个键，全部必需：

| 键（`<AVL>` 换成具体 AVL 名） | 含义 | 取值 |
|---|---|---|
| `<AVL>_BATCH_SIZE` | 单批提交行数 | 正整数 |
| `<AVL>_DATA_EXPIRE_DAYS` | 库表数据保留天数，按 `op_time` 判定 | 正整数 |
| `<AVL>_LOCAL_FILE_EXPIRE_DAYS` | 本地 `.AVL` 保留天数，按 mtime 判定 | 正整数 |
| `<AVL>_POLL_INTERVAL_SECONDS` | 常驻轮询间隔秒数 | 正整数 |
| `<AVL>_PURGE_CYCLE_DAYS` | 库表清理轮转周期 | 非负整数，`0` 合法 |

分表 AVL 再加 `<AVL>_NUM_TABLES`（正整数）：只有 `F09591_NUM_TABLES=128`、`F09633_NUM_TABLES=128` 两个键，由 `config._num_tables()` 按 AVL 名直读。这里要区分两组容易混淆的数字：

- **分表数**：固定表**没有** `<AVL>_NUM_TABLES` 键，代码里 `num_tables=0`（`config.py::load_profiles()` 对非分表 AVL 直接置 0），`validate_specs()` 会拦住「固定表配了 NUM_TABLES」的漂移；分表 AVL 才是 128。
- **清理轮转周期 `<AVL>_PURGE_CYCLE_DAYS`**：所有 12 个 AVL 都有这个键。当前值固定表（10 个）一律 `1`（每天对全部库表清一次），两个分表 AVL 是 `F09591_PURGE_CYCLE_DAYS=7`、`F09633_PURGE_CYCLE_DAYS=7`（每天只清约 1/N 的分表，N 天轮完）。`0` 是「每轮全量清」的合法取值，不是分表数。

也就是说，本节下面「68 个键的完整清单」表里固定表 `NUM_TABLES` 列写 `—` 表示**不配该键**，不是配了 1；`1 / 7` 全部是清理周期，逐键取值见该表，语义见第 11 节「分表 db 的库表清理降频」。

- 粒度是 AVL：`project` 不参与任何键的拼装，没有项目级键、没有项目级兜底、没有任何默认值；漏配一个 AVL 的任一键，启动直接 fail-fast。改完 `.env` 重启进程生效（配置对象冻结）。
- 唯一例外是 `AVL_DROP_BEFORE_INSERT`：全部 AVL 共用的模式开关，必需键、只允许 `0/1`。可另写可选的 `<AVL>_DROP_BEFORE_INSERT` 对单个 AVL 覆盖（缺省回落到全局键），这组覆盖键不计入上面 68 个必需键。
- `AVL_SPECS` 与 `_AVLS` 的 key 必须一致，schema 名必须在 `TABLE_SCHEMAS` 里，主键必须在 `TABLE_PRIMARY_KEYS` 里；分表 AVL 的表结构还须含 `product_no` 列。这些漂移由 `validate_specs()` 启动时拦住。
- `load_profiles(runtime_dir=...)` 或 CLI `--runtime-dir` 是唯一产出隔离入口，只改产出位置、不改配置来源。部署建议 `ln -s /data/crm_runtime sftp_avl2db/runtime` 把数据挂到独立盘，不要整目录覆盖包目录。
- 在线业务侧读取这些 db 时，库路径、表字段与分表数由 `services/db_config.py` 与 `services/.env` 独立声明；两侧键名不必相同，但分表数取值必须一致，改列顺序 / 分表数 / 库路径时两侧声明必须同步。

### 68 个键的完整清单与当前生效值

`sftp_avl2db/.env`（与 `.env.example` 同值、随仓库入库）的实际内容如下。仅凭 `<AVL>_*` 模板无法写出这份文件，重建时照下表逐键落盘；新环境可 `cp .env.example .env` 再替换凭据。

**SFTP 公共 5 项**

| 键 | 当前值 |
|---|---|
| `SFTP_HOST` | `134.80.199.252` |
| `SFTP_PORT` | `22` |
| `SFTP_USERNAME` | `wangsi` |
| `SFTP_PASSWORD` | 见仓库内 `.env` 明文串（此处不转录；部署前必须替换并轮换） |
| `SFTP_REMOTE_PATH` | `/ftpdata2/data_reverse/wangsi/aifile` |

上表 5 个键属外部依赖：获取渠道：<待填写：上游接口人或运维交付渠道>；责任人：<待填写：配置管理员>。本子包补齐缺失行为：`load_profiles()` 对 68 个键逐键读取，漏任一键启动时直接 fail-fast，不回落默认值；`SFTP_PASSWORD` 取值不在此转录，部署前必须替换并轮换。

**全量替换模式 1 项（全部 AVL 共用）**

| 键 | 当前值 | 含义 |
|---|---|---|
| `AVL_DROP_BEFORE_INSERT` | `0` | `0` 关闭 / `1` 开启；开启后写入最新 AVL 前 `DROP` 并重建全部目标表 |

可选覆盖键 `<AVL>_DROP_BEFORE_INSERT`（如 `F09627_DROP_BEFORE_INSERT=1`）：写了就对该 AVL 单独生效，不写则回落到全局键；同样只认 `0/1`。

**12 个 AVL 各 5 项（共 60 项）**

列名是键的后缀，实际键名为 `<AVL>_<列名>`，例如 `F09554_BATCH_SIZE`。

| AVL | BATCH_SIZE | DATA_EXPIRE_DAYS | LOCAL_FILE_EXPIRE_DAYS | POLL_INTERVAL_SECONDS | PURGE_CYCLE_DAYS | NUM_TABLES |
|---|---|---|---|---|---|---|
| `F09554` | 10000 | 3 | 3 | 900 | 1 | — |
| `F09555` | 10000 | 3 | 3 | 900 | 1 | — |
| `F09556` | 10000 | 3 | 3 | 900 | 1 | — |
| `F09591` | 5000 | 3 | 3 | 900 | 7 | 128 |
| `F09627` | 10000 | 365 | 3 | 900 | 1 | — |
| `F09628` | 10000 | 365 | 3 | 900 | 1 | — |
| `F09629` | 10000 | 365 | 3 | 900 | 1 | — |
| `F09630` | 10000 | 365 | 3 | 900 | 1 | — |
| `F09631` | 10000 | 365 | 3 | 900 | 1 | — |
| `F09632` | 10000 | 365 | 3 | 900 | 1 | — |
| `F09633` | 10000 | 365 | 3 | 900 | 7 | 128 |
| `F09635` | 10000 | 365 | 3 | 900 | 1 | — |

**分表 2 项**：`F09591_NUM_TABLES=128`、`F09633_NUM_TABLES=128`。只有这两个 AVL 有 `NUM_TABLES` 键，固定表不配（代码里 `num_tables=0`）。

合计 `5 + 1 + 60 + 2 = 68` 个键，全部必需，按 AVL 逐键配置，没有缺省回落；`load_profiles()` 用 `_int()` / `_str()` / `_flag()` 逐键读取，漏任一键启动时直接 fail-fast。`PURGE_CYCLE_DAYS` 允许 `0`（每轮全量清），`AVL_DROP_BEFORE_INSERT` 只允许 `0/1`，其余数值项均为正整数。诊断 8 张表的 `DATA_EXPIRE_DAYS` 统一为 `365`（商机 / 控损仍为 `3`），`cutoff = now - 365 天`，按 7 天轮转时库内峰值约 `365 + 7 - 1 = 371` 天。

### 全量替换模式（`AVL_DROP_BEFORE_INSERT`）

默认 `0`：`SqliteSink.write()` 仍按主键 `INSERT OR REPLACE` 追加，历史行由 `cleanup_expired()` 按 `DATA_EXPIRE_DAYS` 管控。

设为 `1`：每个 AVL 文件**写入前**，`write()` 先对该 AVL 布局枚举出的全部目标表执行 `DROP TABLE IF EXISTS`，再用同一份建表 DDL 立即重建，然后才写入本批记录。分表 AVL 会一次重建全部 128 张分表；`_avl_meta` 是批次水位表、不进 layout，保留不动。效果是「表里只保留最新 AVL 的数据」，上游不再下发的主键不会残留，也不需要靠新鲜度窗口挡旧行。

- 作用点在 `sftp_avl2db/sink/sqlite_sink.py::write()` / `drop_existing()`，所有调用路径（在线 runner、离线自检直调 `write()`）一致；`runner.import_one()` 会打一行模式日志。
- 同一轮若导入多个日期文件（常驻模式默认同时兜底前一天与当天），每次 `write()` 都会重建一次，最终只剩最后一个文件的数据——这正是「存入最新 AVL 之前清旧」的语义。
- 与过期清理互不冲突：`purge_expired()` 仍按 `DATA_EXPIRE_DAYS` 跑；开启模式只是让表在每批写入前先归零。
- 风险提示：`DROP` 在写入前已提交，若该批解析 / 写入中途失败，表里会是空表或半批数据，下轮需用 `--force-reload` 重跑补齐；建议配合任务锁与失败告警使用。
- 只对部分 AVL 开启时，写 `<AVL>_DROP_BEFORE_INSERT=1` 覆盖全局 `0`，例如只让诊断 8 张表走全量替换。

自检：`python tests/check_drop_before_insert.py`（覆盖关闭 / 开启 / 分表 / 按 AVL 覆盖 / 按业务窗口 / 365 天保留期）。

## 6. 新增业务 / 新增 AVL

1. `table_schemas.py`：每种表结构加一份 `TABLE_SCHEMAS["新名字"]`，并在 `TABLE_PRIMARY_KEYS` 里按同名注册主键（单列或复合）；结构相同的 AVL 直接复用已有名字。
2. `avl_specs.py`：加一条 `AvlSpec`，写明 `project` / `schema` / `parser` / `sharded`；不写表名、主键、PRAGMA、查询条件。设 `sharded=True` 时表结构还须含 `product_no` 列。一行一条的表统一用 `parser="flat"`，列数 / 主键列 / 跳过规则由 profile 注册信息驱动。
3. `config.py::_AVLS`：加一行 `(AVL 名, 项目名)`。
4. `.env`：每个新 AVL 都要加自己的一整套 `<AVL>_*` 键（5 个运行参数），分表 AVL 另加 `<AVL>_NUM_TABLES`；缺任一键启动时直接 fail-fast。
5. 启动 `python -m sftp_avl2db --task {AVL 名}`。若在线业务侧也要读这个新库，在 `services/db_config.py` 补一份在线侧声明（列顺序、db 文件、查询键）。

## 7. AVL 与库表契约

- 业务数据日期 `op_time` 取自 AVL 第 1 列，代码不做日期改写；文件名 `YYYYMMDD` 是文件生成日。补跑时 `--date` 填文件名日期，过期清理按 `op_time` 列。**当前样例数据里 `op_time` 全是文件名日期 - 1 天，但这只是上游事实，不是代码约束**：写入侧不按文件名推算、也不改写日期，在线侧的新鲜度窗口同样以数据里的 `op_time` 为基准。
- 每个 `{AVL}.db` 另有一张 `_avl_meta`（不进 layout、不参与分表路由与过期清理）：`SqliteSink.write()` 成功提交后按文件名日期登记 `latest_file_date` / `latest_op_time`，只在日期不早于已记录值时推进，补跑旧文件不回退；在线侧按同名只读，用来限定查询的新鲜度窗口（见 `services/README.md` 第 4.1 节）。元数据写失败只记 `stats.errors`，不回滚业务数据、不阻断清单登记。
- 表名不用声明：固定表 = `{AVL}`，分表 = `{AVL}_{分表号}`，由 `avl_specs.table_name()` 统一派生。
- **回捞商机**（`F09554/55/56`）：一行一条、无续行，逻辑 12 列；`product_no` 为 `TEXT NOT NULL` 主键，为空跳过；列数不足补空串、超出截断，所有列原样保留文本（末列 `city_id` 声明为 INTEGER，由 SQLite 列亲和性转换）。
- **控损**（`F09591`）：逻辑 5 列，`strategy_name` 含裸换行需续行拼接；`product_no` 为主键与分表键。单个 `F09591.db` 内 128 张表 `F09591_0 … 127`，路由为 `zlib.crc32(product_no.encode("utf-8")) % NUM_TABLES`。写入用 `INSERT OR REPLACE`，批量补跑按文件名日期升序。
- **诊断·用户场景对应关系**（`F09633`）：诊断 8 表里唯一带 `product_no` 的表，千万级行，同样按 `product_no` 分 128 张表 `F09633_0 … 127`，分表数用 `F09633_NUM_TABLES` 单独配置。
- 改列名 / 列顺序只改 `table_schemas.py`，改 AVL 归属 / 解析器只改 `avl_specs.py` 与 `config.py::_AVLS`，改主键只改 `TABLE_PRIMARY_KEYS`，改分表数只改 `.env`；每个 AVL 只有一个 db 文件，不存在跨库路由。分表路由用的 `product_no` 列下标由 `build_layout()` 从注册的表结构推导。

### 商机回捞 / 控损的完整列定义

诊断 8 张表的列清单在第 8 节「诊断 8 张表注册」，这里补齐另两类。`TABLE_SCHEMAS` 的列顺序即 AVL 制表符分隔顺序，也是 `SELECT *` 回来后 `dict(zip(columns, row))` 的组装顺序；在线侧 `services/db_config.py` 声明同列同序。

**`opportunity_recall`（`F09554 / F09555 / F09556` 共用，逻辑 12 列，主键 `product_no`）**

| # | 列名 | 类型 | 说明 |
|---|---|---|---|
| 1 | `op_time` | `TEXT` | 数据日期（AVL 第 1 列，不进主键） |
| 2 | `call_time` | `TEXT` | 通话时间 |
| 3 | `product_no` | `TEXT NOT NULL` | 手机号，主键 / 查询键 |
| 4 | `type_dl` | `VARCHAR(50)` | 大类 |
| 5 | `type_xl` | `VARCHAR(50)` | 小类（商机「用户需求」蓝色前缀） |
| 6 | `content_abstract` | `TEXT` | 通话总结 |
| 7 | `cust_demand` | `TEXT` | 用户需求 |
| 8 | `rec_strategy` | `TEXT` | 已推荐政策 |
| 9 | `reject_reason` | `TEXT` | 拒绝原因 |
| 10 | `reason_note` | `TEXT` | 具体表述 |
| 11 | `refer_strategy` | `TEXT` | 参考策略（在线话术不消费） |
| 12 | `city_id` | `INTEGER` | 地市；解析器保留文本，由 SQLite 列亲和性转换 |

在线侧只消费第 5、6、7、8、9、10 列，见 [`services/README.md` 第 9 节](../services/README.md)。解析器 `FlatColumnParser`：一行一条、无续行，按 `\t` 切分后补齐或截断到 12 列，所有列原样保留文本；空 `product_no` 跳过。

**`loss_control`（`F09591`，逻辑 5 列，主键 `product_no`，分表键同列）**

| # | 列名 | 类型 | 说明 |
|---|---|---|---|
| 1 | `op_time` | `TEXT` | 数据日期 |
| 2 | `user_id` | `TEXT` | 用户编码 |
| 3 | `product_no` | `TEXT NOT NULL` | 手机号，主键 / 分表键 / 在线查询键 |
| 4 | `city_id` | `TEXT` | 地市 |
| 5 | `strategy_name` | `TEXT` | 含裸换行的自定义标签话术；**在线侧唯一消费列** |

续行解析（`parsers/continuation.py`）：以行首 `YYYY-MM-DD` 判定新记录；`strip_eol()` 只去 `\n`、再只剥**一个** ASCII 反斜杠（不能用 `rstrip('\\')`，否则 GBK 尾字节 `0x5C` 的汉字会被削成乱码）；起始行 `>=5` 列取前 5 列、`==4` 列补空 `strategy_name`、`<4` 列整条跳过；空物理行与文件开头孤立续行跳过；最后一条单独入库。单个 `F09591.db` 内 128 张表 `F09591_0 … 127`，路由 `zlib.crc32(product_no.encode("utf-8")) % NUM_TABLES`，写入用 `INSERT OR REPLACE`，批量补跑按文件名日期升序。

两类库都以主键 + `INSERT OR REPLACE` 保证幂等，`op_time` 不进主键（每日全量覆盖最新批次）；所有 SQLite 连接只设 `PRAGMA journal_mode = WAL` 与 `PRAGMA synchronous = NORMAL`。

## 8. 诊断 8 张表注册（project = diagnosis）

| AVL | 中文名 | schema | 列（按 AVL 顺序） | 主键 |
|---|---|---|---|---|
| `F09627` | 场景优先级 | `scene_rank` | `op_time, scene, rn` | `scene` |
| `F09628` | 产品-场景 | `product_scene` | `op_time, business_type, scene` | `business_type, scene` |
| `F09629` | 产品策略 | `product_business` | `op_time, business_type, product_id, business_name` | `product_id` |
| `F09630` | 场景-切入话术 | `scene_qrhuashu` | `op_time, scene, qr_huashu` | `scene` |
| `F09631` | 产品-营销话术 | `product_yxhuashu` | `op_time, business_type, yx_huashu` | `business_type, yx_huashu` |
| `F09632` | 产品-挽留话术 | `product_wlhuashu` | `op_time, business_type, reject_reason, wl_huashu` | `business_type, reject_reason, wl_huashu` |
| `F09633` | 用户场景对应关系（**分表**） | `conf_user_scene` | `op_time, user_id, product_no, scene, reason` | `product_no, scene` |
| `F09635` | 标签ID-标签名 | `tagid_tagname` | `op_time, tag_id, tag_name, unknown1, unknown2` | `tag_name` |

主键口径：`op_time` 不进主键，维表每日全量按主键 `INSERT OR REPLACE` 覆盖最新批次；主键列自动补 `NOT NULL`。`F09635` 以 `tag_name` 为主键（在线侧查询键同）：诊断话术里出现的是 `#标签名&`，按标签名反查 `tag_id`（= IOP 的 `ITEMCODE`）才能取到 `ITEMVALUE`，写入侧与在线侧 `TABLE_PRIMARY_KEYS` 同步。**部署提示**：`CREATE TABLE IF NOT EXISTS` 不会改写已有表的主键。`F09631` / `F09632` 把话术全文纳入主键，避免重复行或「同一产品 + 同一拒绝原因对应两条不同挽留话术」被 REPLACE 丢行。`F09633` 主键 `(product_no, scene)` 与分表键共用 `product_no`，同一号码一定落在同一分表。`F09629` 的 `business_name` 含【】内外文本，业务侧按【】内为资费名称、【】外为资费摘要拆分。

离线复跑（8 个 AVL 各 10 行样例，日期按文件名取 `20261120`）：

```bash
for t in F09627 F09628 F09629 F09630 F09631 F09632 F09633 F09635; do
  python -m sftp_avl2db --task $t --date 20261120 \
    --local-dir io_samples --runtime-dir /tmp/replay
done
```

离线复跑仍走 `python -m sftp_avl2db`，会装配 `TaskLogger`，因此**必须先装 `loguru`**；`--local-dir` 只替换远端源，不绕过日志依赖。未装时退出码 2，报「TaskLogger 默认使用 loguru；请安装 loguru，或注入 logger=InMemoryLogger() / 宿主 logger 对象」。`python -m` 入口本身没有 logger 参数，只有编程式调用 `cli.main(logger=...)` 或 `build_task(logger=InMemoryLogger())` 才能注入替身；`tests/check_diagnosis_offline.py` 走的就是注入路径，所以它不依赖 loguru。

在线业务侧的取数链路见 [`services/README.md`](../services/README.md)。

## 9. 解析与写入行为

1. `FlatColumnParser` 是唯一的 flat 解析器，商机回捞与诊断 8 张维表共用：按 UTF-8 读取（`errors="ignore"`），只去行尾换行 / BOM、保留首尾制表符与空列，按 `\t` 切分后补齐或截断到 `profile.columns` 声明的列数。所有列原样保留文本、不做数值转换；空主键判定的列下标由 `build_parser()` 按 `profile.primary_key` 换算。
2. 控损续行：以行首 `YYYY-MM-DD` 判定新记录；`strip_eol()` 只去 `\n`、再只剥**一个** ASCII 反斜杠（不能用 `rstrip('\\')`，否则 GBK 尾字节 `0x5C` 的汉字会被削成乱码）；起始行 `>=5` 列取前 5 列、`==4` 列补空 `strategy_name`、`<4` 列整条跳过；空物理行与文件开头孤立续行跳过；最后一条单独入库。
3. `sink/schema.py` 建表 SQL 保持表级 `PRIMARY KEY (...)` 声明形式；主键列由 `TABLE_PRIMARY_KEYS` 传入，可单列可复合，自动补 `NOT NULL`。
4. 所有 SQLite 连接统一只设 `PRAGMA journal_mode = WAL` 与 `PRAGMA synchronous = NORMAL`；写入按 `BATCH_SIZE` 分桶、逐桶满批提交，使用 `INSERT OR REPLACE` + 主键幂等。
5. 分表口径 `zlib.crc32(product_no) % NUM_TABLES`；`p_no` 为空时取 `"NULL"`（解析器已跳过空主键，属不可达分支）。

## 10. 拉取不变量

- `runtime/state/{AVL}.fetched` 记录已成功入库的远程文件名：每轮先跳过清单命中者，入库成功后才登记；失败不登记、本地文件保留、下一轮自动重试。
- `runtime/state/{AVL}.lock` 非阻塞单实例锁，拿不到立即失败；平台无可用锁库时明确报错。
- 所有任务按 `TABLE_PRIMARY_KEYS` 注册的主键 + `INSERT OR REPLACE` 保证幂等：商机回捞 / 控损为 `product_no`，诊断维表为各自业务定位键（可复合）。
- `exists()` 三态：`False` → INFO「远程文件尚未生成」；`FileNotFoundError` → 同 `False`；其他异常 → ERROR「stat 远程文件出错」。后两者跳过、不登记清单。

## 11. 分表 db 的库表清理降频

`op_time` 不在主键里、没有索引，主键又是 `INSERT OR REPLACE` 覆盖最新批次，过期清理的成本主要在「全表扫」。用每个 AVL 自己的 `<AVL>_PURGE_CYCLE_DAYS` 降频：

| 取值 | 行为 |
|---|---|
| `0` | 每轮全量清（不降频） |
| `1` | 每天对全部库表清一次 |
| `N` | 每天只清约 `1/N` 的分表，`N` 天轮完一圈 |

- 每个 AVL 一个键，必需、无兜底；固定表不参与轮转，仍每天全清一次。
- 选表无状态：按日历日序号取模，把布局枚举出的分表按下标切成互斥的 `N` 组，`i % N == 日序号 % N` 的当天清。128 张表配 `N=7` 分成 19/19/18/18/18/18/18 七组，组间不重叠、并集恰好覆盖全部分表；`N` 大于表数时收敛到表数。
- 每天只跑一次：闸门只记进程内的日历序号，不新增状态文件；进程重启当天最多重复清一次。
- 本地 AVL 文件清理仍每轮执行，与库表清理解耦。
- 单次 `--date` 补跑强制全量清（`cleanup_expired(force_full=True)`）。
- `purge_expired(cutoff, tables=None)` 不传 `tables` 时全量清；传 `tables` 时只清指定表（测试替身与离线复跑用）。

每张表每 `N` 天判定一次，`cutoff` 按清理当天算，库内峰值约 `DATA_EXPIRE_DAYS + N - 1` 天；清理当天事务和 WAL 会比日常大。保留期短的分表库建议配 `1~2`，保留期长的配 `7`。主键 REPLACE 下在线侧按主键定位单表 / 单行，晚删的旧行不影响查询结果。

```bash
python tests/check_purge_rotation.py
```

断言：AVL 粒度配置（缺键 fail-fast、逐 AVL 独立取值）、7 组并集覆盖 128 张表且组间互斥、`cycle=0` 行为、固定表每日全清、每天只触发一次 `purge_expired()`、跨 7 天过期行全部清干净且新鲜行保留、补跑一次全量清。

## 12. 测试与复跑

```bash
python -m sftp_avl2db --task F09591 --date 20261110 \
  --local-dir io_samples --runtime-dir /tmp/replay
python tests/check_diagnosis_offline.py   # 端到端自检（写入侧与在线侧配置独立装配）
python tests/check_purge_rotation.py     # 清理轮转自检
```

上面的 `python -m sftp_avl2db` 命令需要 `loguru`；`--local-dir` 只换源，不绕过日志依赖。两个 `tests/check_*.py` 走 `load_profiles()` 与注入 logger，不需要 loguru。

`build_task()` 的 `logger=`、`local_dir=`、`day=`、`now=`、`sleeper=`、`batch_size=`、`table_schema=` 都是显式注入点；`runner.serve_forever()` 另有 `max_rounds` / `date_strings` 测试钩子。离线复跑产出隔离到临时目录，不碰包内 `runtime/`。
