# sftp_avl2db · SFTP AVL → SQLite 业务库

两个 SFTP AVL 拉取入库任务的写入侧代码包，可独立复用。**配置与运行时产出都在包内，都用 `__file__` 定位，与启动时的 CWD 无关**：`sftp_avl2db/.env` 是唯一配置来源，`sftp_avl2db/runtime/` 是唯一的运行时数据目录。

`import sftp_avl2db` 零 I/O、零第三方依赖（不读 `.env`、不连网、不建目录、不改宿主 logger）。组件按 **配置 / 解析 / 写入 / I-O / 编排** 五层拆分，每层可单独 import、替换：`config.py` + `dbspec.py`（配置）、`parsers/`（解析器）、`sink/`（布局与 SQLite 写入）、`remote.py` / `state.py` / `logging.py`（外部依赖，延迟导入）、`runner.py` / `profiles.py` / `cli.py`（编排与入口）。在线侧 `services/data_query.py` 只读查询，不参与写入。

> 把包 copy 到别的项目时，**排除 `.env` 与 `runtime/`**，从 `.env.example` 起步，避免把 A 项目的凭据和业务库一起带走。

## 任务与运行

| 业务 | 任务 key（`--task`） | `.env` 前缀 | AVL 文件前缀 |
|---|---|---|---|
| 回捞商机 | `opportunity_recall` | `OPPORTUNITY_RECALL_` | F09554 / F09555 / F09556 |
| 控损 | `loss_control` | `LOSS_CONTROL_` | F09591 |

任务 key 同时是 `DB_SCHEMAS` / `QUERY_SPECS` 的 key、`runtime/sqlite_db/{任务名}/` 库目录、`.env` 键前缀、日志与状态文件名前缀；`F09554` 等文件前缀与 `data_F09554` 等表名属外部数据约定，保持不变。

```bash
python -m sftp_avl2db --task opportunity_recall
python -m sftp_avl2db --task loss_control --date 20261110
python -m sftp_avl2db --task loss_control --date 20261110 --force-reload
```

`--date YYYYMMDD` 为单次模式（拉完指定**文件名日期**后退出），不传则常驻轮询；`--force-reload` 强制重下重入库。离线复跑用 `--local-dir io_samples`（把本地目录当远程，不连 SFTP）配合 `--runtime-dir <临时目录>`，产出隔离到临时目录、**不碰包内 `runtime/`**。生产每个任务各起一个常驻进程，靠 `runtime/state/{任务名}.lock` 保证单机单实例。

远程文件为 `{业务前缀}{YYYYMMDD}000000.AVL`，制表符分隔、UTF-8；每轮同时检查「昨天 + 今天」两天的文件名。本地文件落在 `runtime/files/{任务名}/`，数据、本地文件按 `.env` 的保留天数清理；日志 `runtime/log/{任务名}_INFO|ERROR_日期.log` 按天轮转、保留 7 天。

## 配置

**唯一来源 `sftp_avl2db/.env`，唯一入口 `load_profiles()`。** 不读系统环境变量，没有 `TASKS_ENV_FILE` / `--env` 路径覆盖，也没有 `parse_dotenv()` / `load_values()` / `from_mapping()` 等多套入口。改完 `.env` **重启进程**生效（配置对象冻结）。

| 文件 | 作用 |
|---|---|
| `sftp_avl2db/.env` | 与 `config.py` 同目录，17 个键；日常改这个 |
| `sftp_avl2db/.env.example` | 脱敏模板，新环境 `cp sftp_avl2db/.env.example sftp_avl2db/.env` 后按实际填写 |
| `config.py` 的 `_TASKS` | 任务声明表（任务名 / 键前缀 / 解析器 / 是否分表）；新增任务加一行 + 写进 `.env` |
| `dbspec.py` | 包内静态契约（纯数据、不读 `.env`）：`DB_SCHEMAS` 既是写入侧建表定义，也是读取侧 `SELECT *` 组装 dict 的列顺序；`QUERY_SPECS` 为在线查询声明；`QUERY_MODES` 为库表形式枚举；`validate_specs(profiles)` 显式校验配置漂移，不合法抛 `ValueError` |

`.env` 语法只认**整行 `#` 注释**和 `KEY=VALUE`，不做引号、行内注释或 `${VAR}` 插值。**缺必需键、非正整数、文件不存在都直接报错**，不会静默用默认值——`FILE_PREFIXES` / `NUM_TABLES` 配错会让读写两侧算出不同库表，必须 fail-fast。

想确认改没生效（`repr` 会自动屏蔽 `SFTP_PASSWORD`）：

```bash
python -c "from sftp_avl2db import load_profiles; print(load_profiles())"
```

### 运行时目录

`load_profiles()` **不传参** → 产出落在包内 `runtime/`（`sqlite_db` / `state` / `files` / `log`）。`services/data_query.py` 永远不传参，读写同源就靠这一条保证。

`load_profiles(runtime_dir=...)` 或 CLI `--runtime-dir DIR` 是**唯一隔离入口**：只改产出位置、不改配置来源，用于离线复跑或把数据挂到独立磁盘：

```bash
# 推荐：包内路径不变，数据实际落在数据盘，发版覆盖不会碰它
ln -s /data/crm_runtime sftp_avl2db/runtime
```

⚠️ **部署时不要 `rm -rf sftp_avl2db/` 再整目录覆盖**——包目录现在是代码 + 数据盘。只同步 `*.py` 与 `.env.example`，或按上面做软链。

**不沿用项目总 `config.yaml`**；`.env` 含 SFTP 明文密码，仓库里只提交 `.env.example`，`.env` 与 `runtime/` 都在根 `.gitignore` 中。

`.env` 分组：SFTP 公共 5 项（`SFTP_HOST/PORT/USERNAME/PASSWORD/REMOTE_PATH`），每个任务一组 `<业务前缀>_FILE_PREFIXES / BATCH_SIZE / DATA_EXPIRE_DAYS / LOCAL_FILE_EXPIRE_DAYS / POLL_INTERVAL_SECONDS`，控损另有 `NUM_TABLES` / `TABLE_NAME_PREFIX`。当前取值：

| 键（省略任务前缀） | 回捞商机 | 控损 |
|---|---|---|
| `FILE_PREFIXES` | F09554,F09555,F09556 | F09591 |
| `BATCH_SIZE` | 10000 | 5000 |
| `DATA_EXPIRE_DAYS` | 30 | **3** |
| `LOCAL_FILE_EXPIRE_DAYS` | 7 | **3** |
| `POLL_INTERVAL_SECONDS` | 600 | 1800 |
| `NUM_TABLES` / `TABLE_NAME_PREFIX` | — | 128 / `data_F09591` |

`PROFILES` / `DB_SCHEMAS` / `QUERY_SPECS` 三者 key 必须完全一致；`mode` 以 `QUERY_SPECS` 为唯一真源，`load_profiles()` 派生到 profile，`validate_specs()` 拦住手填漂移。

## AVL 与库表契约

**业务数据日期 `op_time` 取自 AVL 第 1 列，代码不做日期改写**；文件名中的 `YYYYMMDD` 是文件生成日，回捞商机对应 T-2、控损对应 T-1。补跑时 `--date` 填文件名日期，不是 `op_time`；过期清理按 `op_time` 列。

### 回捞商机（F09554 / F09555 / F09556）

一行一条记录、无续行，逻辑 12 列：`op_time, call_time, product_no, type_dl, type_xl, content_abstract, cust_demand, rec_strategy, reject_reason, reason_note, refer_strategy, city_id`。`product_no` 为 `TEXT NOT NULL` 主键与在线查询键，为空则跳过；列数不足补空串、超出截断；`city_id` 非数字或空 → 0。

每个前缀一个库 `runtime/sqlite_db/opportunity_recall/{F09554,F09555,F09556}.db`，表名 `data_{前缀}`，`PRIMARY KEY (product_no)`；`QUERY_SPECS["opportunity_recall"]["db_map"]` 把 product_id 映射到库名（库名必须存在于 `FILE_PREFIXES`）。

### 控损（F09591）

逻辑 5 列：`op_time, user_id, product_no, city_id, strategy_name`（`TEXT NOT NULL` 的 `product_no` 为主键、分表键、查询键；`strategy_name` 含裸换行，需续行拼接）。续行规则：

1. 行首 `YYYY-MM-DD` 的物理行是一条新逻辑记录起始，其余物理行是上一条第 5 列的续行，用 `\n` 拼回。
2. 行尾只剥**一个** ASCII 反斜杠；不能用 `rstrip('\\')`，否则 GBK 尾字节为 `0x5C` 的汉字会被削掉一个字节变成乱码。
3. 起始行按 `\t` 切分：`>=5` 列取前 5 列（第 6 列起丢弃），`==4` 列补空 `strategy_name`，`<4` 列为损坏行跳过；空物理行跳过；文件开头无上文的续行丢弃；读完最后一条单独入库。
4. 空 `product_no` 记录跳过。

单库 `runtime/sqlite_db/loss_control/F09591.db`，128 张表 `data_F09591_0 … data_F09591_127`，路由 **`表号 = zlib.crc32(product_no.encode("utf-8")) % NUM_TABLES`**，必须与读取侧 `services/data_query.py::_resolve()` 完全一致。写入用 `INSERT OR REPLACE`（`PRIMARY KEY (product_no)`，后写覆盖先写），主键自带索引，不再额外建 `(product_no, op_time)` 索引；批量补跑历史文件按文件名日期**升序**执行。

### 库表形式（`mode`）

`mode` 按「库数量 × 表数量」划分，仅 `QUERY_MODES` 三种，**多库多表不存在**；写入侧 `sink/router.py::build_layout()` 与读取侧 `_resolve()` 按同一组值分支：

| mode | 库文件 | 表名 | 专用字段 |
|---|---|---|---|
| `multi_db_single_table` | 按 product_id 选 `{库名}.db` | 每库一张 `data_{库名}` | `db_map`（库名映射，不是表名） |
| `single_db_multi_table` | `{FILE_PREFIXES[0]}.db` | `{TABLE_NAME_PREFIX}_{crc32 % NUM_TABLES}` | 用 `.env` 分表配置 |
| `single_db_single_table` | `{FILE_PREFIXES[0]}.db` | `table_name` 指定的固定表 | `table_name`（已实现，当前无任务使用） |

改列名/列顺序只改 `DB_SCHEMAS`，改条件或排序只改 `QUERY_SPECS`，改分表数/前缀只改 `.env`；读写两侧同时生效。

## 拉取不变量

- `runtime/state/{任务名}.fetched` 记录已成功入库的远程文件名；每轮先跳过清单命中者，**入库成功后才登记**，失败不登记、本地文件保留、下一轮自动重试；`--force-reload` 连清单命中也强制重下。
- 每轮检查「昨天 + 今天」两天文件名做跨天兜底。
- `runtime/state/{任务名}.lock` 为非阻塞单实例锁，拿不到锁立即失败，避免两个拉取者并发出库。
- 所有任务以 `product_no` 为主键 + `INSERT OR REPLACE` 保证入库幂等，同一手机号只保留一条。

## 在线读取与话术重组

`services/data_query.py` 按任务名派发：`query(task_name, telnum, product_id=None)` 及薄封装 `get_opportunity_recall_latest()` / `get_loss_control_latest()`，均为 `async` 函数、内部同步 `sqlite3`，`SELECT * … LIMIT 1` 并按 `op_time DESC` 取最新一条；表名与列名来自代码内配置，用户输入只走参数绑定。库/表缺失、product_id 未映射、任务名未注册或查询异常一律返回 `{}`，不会让 API 500。

**库里保存原始文本**，话术重组在业务侧执行：`rewrite_by_spec_f09554` 拼【通话总结】【用户需求】【已推荐政策】【拒绝原因】四段并追加辅助画像；`rewrite_by_spec_f09591` 把 `<$B>` / `<$I>` / `<$U>` / `<#RRGGBB>` 等自定义标签转成 HTML 标签、按栈闭合 `</>`。

以下解析行为刻意保真，不要顺手优化：商机编码探测带 `errors="ignore"` 且实际恒走 `utf-8` 分支；控损续行只剥一个反斜杠、起始行 `>=5` 列时丢弃第 6 列起；分表口径 `zlib.crc32(product_no) % NUM_TABLES` 读写两侧必须一致。
