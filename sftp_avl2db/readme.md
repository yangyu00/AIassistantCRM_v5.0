# sftp_avl2db · 用法与结构

> **文档地图**：`sftp_avl2db/` 的用法说明。
> 同包另一份：[`constraints.md`](constraints.md)（约束）；全局架构与在线业务：[根 `README.md`](../README.md)。
> - [`IOP/readme.md`](../IOP/readme.md) · [`IOP/constraints.md`](../IOP/constraints.md)
> - [`local_excel2db/readme.md`](../local_excel2db/readme.md) · [`local_excel2db/constraints.md`](../local_excel2db/constraints.md)

> 2 个 SFTP AVL 拉取任务的写入侧代码包，可独立复用。本文件讲任务命名、组件分层、配置、两种 AVL 的字段与库表形式、以及在线侧如何按 `PROFILES` / `QUERY_SPECS` 只读查询；幂等与重试等不变量见 `constraints.md`。

## 任务命名

两个任务按业务含义命名，不再用「AVL 前缀 + 缩写」组合。同一个英文名同时作为
`TaskProfileConfig.name` / `sftp_avl2db.dbspec.DB_SCHEMAS` / `QUERY_SPECS` 的 key、
`tasks/sqlite_db/{任务名}/` 库目录名、`.env` 键前缀、日志与状态文件名前缀。

| 中文业务名 | 任务 key | `.env` 键前缀 | 启动命令（`--task` 值） | AVL 前缀 |
|---|---|---|---|---|
| 回捞商机 | `opportunity_recall` | `OPPORTUNITY_RECALL_` | `python -m sftp_avl2db --task opportunity_recall` | F09554 / F09555 / F09556 |
| 控损 | `loss_control` | `LOSS_CONTROL_` | `python -m sftp_avl2db --task loss_control` | F09591 |

> `F09554` 等 AVL 文件前缀与 `data_F09554` 等表名属于外部数据约定，保持不变；
> 只有任务自身的 key、脚本名、配置键前缀、库目录与文件名前缀采用业务英文名。

## 通用规则（用法）

> 条目编号沿用原「通用规则」；约束类条目（2–5）见 `constraints.md`。

1. **远程文件**：连接参数见 `.env` 的 `SFTP_*`。
   每天生成 `{业务前缀}{YYYYMMDD}000000.AVL`，制表符分隔，UTF-8。

6. **轮询间隔**：由 `.env` 各任务分组的 `*_POLL_INTERVAL_SECONDS` 控制（业务前缀见「任务命名」，默认 600 / 1800 秒）。
7. **本地文件**：`tasks/files/{任务名}/`，按 `.env` 各任务分组的 `*_LOCAL_FILE_EXPIRE_DAYS` 天清理。
8. **日志**：`tasks/log/{任务名}_INFO|ERROR_日期.log`，每天轮转，保留 7 天。
9. **运行方式**（在项目根目录执行，用 `-m` 保证包内相对导入生效）：
   ```bash
   python -m sftp_avl2db --task opportunity_recall
   python -m sftp_avl2db --task loss_control --date 20261110
   python -m sftp_avl2db --task loss_control --date 20261110 --force-reload
   ```
   `--date YYYYMMDD` 单次模式（拉完指定**文件名日期**后退出，不传则常驻轮询）；
   `--force-reload` 强制重下重入库。
   离线复跑 / 验证可加 `--local-dir io_samples`（把本地目录当远程，不连 SFTP）、
   `--env` / `--project-root` 指定配置与运行时根目录。
   生产建议用 systemd/nohup 各起一个常驻进程；单机单实例（`tasks/state/{任务名}.lock`）。

## 组件分层与独立复用（`sftp_avl2db/`）

写入侧拆成 5 层，**每层都能单独 import、单独测试、单独替换**；`import sftp_avl2db`
本身零 I/O、零第三方依赖（不读 `.env`、不连网、不建目录、不改宿主 logger）。

| 层 | 模块 | 主要类 / 函数 | 第三方依赖 |
|---|---|---|---|
| 配置 | `sftp_avl2db/config.py` + 包内 `sftp_avl2db/dbspec.py` | `TaskProfileConfig`（冻结 dataclass）、`load_profiles()`、`from_mapping()`、`DB_SCHEMAS` / `QUERY_SPECS` / `QUERY_MODES` / `validate_specs()` | 无 |
| 解析 | `sftp_avl2db/parsers/` | `FlatColumnParser` / `ContinuationParser`（生成器 `yield`，不装全量记录）、`build_parser()` | 无 |
| 写入 | `sftp_avl2db/sink/` | `TableSchema`（DDL / `INSERT OR REPLACE` / 过期 DELETE）、`TableLayout`（协议 + `PerPrefixLayout` / `Crc32ShardLayout` / `FixedTableLayout`，按 `mode` 分派）、`SqliteSink`（建表 / 分桶分批 / 过期清理）、`build_layout()` / `build_sink()` | 仅标准库 `sqlite3` |
| I/O | `sftp_avl2db/remote.py` / `state.py` / `logging.py` | `SftpRemoteSource` / `LocalDirRemoteSource`、`FetchedManifest` / `SingleInstanceLock`、`TaskLogger` / `InMemoryLogger` | 延迟导入；离线组件零依赖 |
| 编排 | `sftp_avl2db/runner.py` / `profiles.py` / `cli.py` | `Avl2DbRunner`（`import_one` / `run_once_for_date` / `serve_forever` / `cleanup_expired`）、`build_task()` | 无 |

在其他项目里按需取用，不必接整条链路：

```python
from sftp_avl2db import TaskProfileConfig, build_parser, build_sink, build_layout
p = TaskProfileConfig.from_mapping({...})          # 不需要 .env
layout = build_layout(p)
sink = build_sink(p, layout=layout)
sink.ensure_schema()
sink.write("F09591xxx.AVL", build_parser(p).parse(path), layout=layout)
```

以下行为刻意保留，不要顺手优化：商机编码探测带 `errors="ignore"` 实际恒走
utf-8；控损 `_strip_eol()` 只剥**一个**反斜杠，且 `>=5` 列时丢弃第 6 列起。
分表口径 `zlib.crc32(product_no) % NUM_TABLES` 必须与读取侧
`services/data_query.py::_resolve()` 一致。

## 配置

- 配置分两处，**都是显式加载，import 时零 I/O**：
  - `sftp_avl2db/dbspec.py`（包内静态契约）：`DB_SCHEMAS[任务名]` / `QUERY_SPECS[任务名]` /
    `QUERY_MODES`，纯数据、不读 `.env`，写入侧建表与读取侧查询共用。
  - `sftp_avl2db/config.py`（运行时）：`parse_dotenv()` / `load_values()` 解析项目根 `.env`，
    `load_profiles()` 构造冻结的 `TaskProfileConfig`（`name` / `file_prefixes` / `sftp` /
    `schema` / `parser` / `mode` / `num_tables` / `table_name_prefix` / `retention` /
    `log_dir` / `state_dir` / `local_dir` / `db_dir` / `fetched_path` / `lock_path`）。
- **不沿用项目总的 `config.yaml` / `config.py`**；运行时配置只在
  `sftp_avl2db/config.py` 一处定义，静态契约只在 `sftp_avl2db/dbspec.py` 一处定义。
- `services/data_query.py` 在模块加载时 `load_profiles()` 一次，从同一份
  `TaskProfileConfig.db_dir` 取库目录（固定指向 `tasks/sqlite_db/{任务名}/`），
  与写入侧路径一致，读写不再各算一套。
- 静态契约的内容与校验：
  - `DB_SCHEMAS[任务名]`：`(列名, 类型)` 列表，即建表列定义，也是 `SELECT *` 结果的 `dict` 组装顺序；
    `TableSchema.from_profile()` 直接引用它，不再各写一份。
  - `QUERY_SPECS[任务名]`：在线查询声明（`mode` / `where` / `order_by` / `db_map` / `table_name` / `product_ids`）；
    `mode` 分多库单表 / 单库多表 / 单库单表三种形式（多库多表不存在），详见本文「`mode` 的三种库表形式」小节。
    **它是库表形式的唯一真源**：`TaskProfileConfig.mode` 由 `load_profiles()` 从这里派生，
    写入侧 `build_layout()` 与读取侧 `_resolve()` 按同一组值分支，不再有第二套名字。
  - `sftp_avl2db.dbspec.validate_specs(profiles)` 显式校验：`where` 列必须存在于 `DB_SCHEMAS`、
    `db_map` 的库名必须存在于 `file_prefixes`、单库多表模式必须有 `num_tables` / `table_name_prefix`、
    单库单表模式必须有 `table_name`，`mode` 不在三种形式内也直接抛 `ValueError`；
    另外校验 `profile.mode` 与 `QUERY_SPECS[].mode` 一致，拦住 `from_mapping()` 手填漂移。
    写入侧 `load_profiles()` 与读取侧 `data_query` 各自调用一次；**import 时不再自动校验**。
- 优先级：**系统环境变量 > `.env`**；设置 `TASKS_ENV_FILE` 可指向其他 `.env` 路径，
  CLI 也可用 `--env` / `--project-root` 指定。
  `.env` 支持 `#` 注释、行内注释、`export`、`${VAR}` 插值、单/双引号；缺必需键会在启动时报错，不会静默连错服务器。
  其他项目不带 `.env` 时用 `TaskProfileConfig.from_mapping({...})` 直接构造。
- 分组：SFTP 公共 5 项（`SFTP_HOST/PORT/USERNAME/PASSWORD/REMOTE_PATH`），
  每个任务一组 `<业务前缀>_*`（前缀列表、批次大小、库/本地文件保留天数、轮询间隔；业务前缀见「任务命名」），
  控损另有 `NUM_TABLES`、`TABLE_NAME_PREFIX`。

  默认值如下（表中键名省略前缀 `OPPORTUNITY_RECALL_` / `LOSS_CONTROL_`，分表键同理）：

  | 键 | 回捞商机 | 控损 |
  |---|---|---|
  | `FILE_PREFIXES` | F09554,F09555,F09556 | F09591 |
  | `BATCH_SIZE` | 10000 | 5000 |
  | `DATA_EXPIRE_DAYS` | 30 | **3** |
  | `LOCAL_FILE_EXPIRE_DAYS` | 7 | **3** |
  | `POLL_INTERVAL_SECONDS` | 600 | 1800 |
  | `NUM_TABLES` / `TABLE_NAME_PREFIX` | — | 128 / `data_F09591` |

  > 控损保留天数为 **3 天**，由 `.env` 的 `LOSS_CONTROL_DATA_EXPIRE_DAYS` 控制。
- `.env` 含 SFTP 明文密码：仓库公开时建议加入 `.gitignore`，或部署时改用系统环境变量注入（不提交 `.env`）。
- 运行时配置不并入项目总 `config.yaml`，避免写入侧与在线业务的配置耦合。
- `tests/fixtures/legacy_specs.json` 是**去敏感的配置冻结快照**（不含密码），
  用于回归锁定「配置逐字段不漂移」；交付包 `baseline/` 下的脚本可在零第三方
  依赖环境下复跑 golden 与全量库表比对。

## 回捞商机（号卡 / 套餐 / 宽带）AVL 入库（F09554 / F09555 / F09556）

进程：`python -m sftp_avl2db --task opportunity_recall`；任务名 `opportunity_recall`。

### 文件日期与数据日期

**op_time = 文件名中 YYYYMMDD 的前两天（T-2）**，直接取自文件第 1 列，代码不做日期改写。

| 文件名 | 文件日期 | 入库 op_time |
|---|---|---|
| `F0955420261110000000.AVL` | 2026-11-10 | **2026-11-08** |
| `F0955520261110000000.AVL` | 2026-11-10 | **2026-11-08** |
| `F0955620261110000000.AVL` | 2026-11-10 | **2026-11-08** |

### 字段（12 列，一行一条，无续行）

| # | 字段 | 类型 | 说明 |
|---|---|---|---|
| 1 | op_time | TEXT | 数据日期 = 文件名日期 - 2 天；过期清理按此列 |
| 2 | call_time | TEXT | 通话日期 |
| 3 | product_no | TEXT NOT NULL | 手机号（**主键**、在线查询的键） |
| 4 | type_dl | VARCHAR(50) | 大类（如「号卡业务」） |
| 5 | type_xl | VARCHAR(50) | 小类（如「新开卡需求」） |
| 6 | content_abstract | TEXT | 通话摘要 |
| 7 | cust_demand | TEXT | 客户需求 |
| 8 | rec_strategy | TEXT | 推荐策略 |
| 9 | reject_reason | TEXT | 拒绝原因 |
| 10 | reason_note | TEXT | 原因备注 |
| 11 | refer_strategy | TEXT | 参考策略 |
| 12 | city_id | INTEGER | 地市编码；非数字/空 → 0 |

- 列数不足补空串，超出截断；`product_no` 为空的记录跳过。
- 库表：每个前缀一个库 `tasks/sqlite_db/opportunity_recall/{F09554,F09555,F09556}.db`，
  表名 `data_{前缀}`，主键 `PRIMARY KEY (product_no)`；与在线查询的
  `QUERY_SPECS["opportunity_recall"]["db_map"]`（202608041615335173/172/174 → 库名）对应。
- 保留天数、批次大小、轮询间隔见 `.env`（`OPPORTUNITY_RECALL_*`）。
- 量级：每个 AVL 每天不到 1 万条，单库单表即可；覆盖后每库行数 ≤ 去重后的手机号数。

## 控损 AVL 入库（F09591）

进程：`python -m sftp_avl2db --task loss_control`；任务名 `loss_control`。

### 文件日期与数据日期

**op_time = 文件名中 YYYYMMDD 的前一天（T-1）**，直接取自文件第 1 列，代码不做日期改写。

| 文件名 | 文件日期 | 入库 op_time |
|---|---|---|
| `F0959120261110000000.AVL` | 2026-11-10 | **2026-11-09** |

### 字段（逻辑 5 列）

| # | 字段 | 类型 | 说明 |
|---|---|---|---|
| 1 | op_time | TEXT | 数据日期 = 文件名日期 - 1 天；过期清理按此列 |
| 2 | user_id | TEXT | 用户标识 |
| 3 | product_no | TEXT NOT NULL | 手机号（**主键**、分表键、查询键） |
| 4 | city_id | TEXT | 地市编码 |
| 5 | strategy_name | TEXT | 控损策略正文（**含裸换行，需续行拼接**） |

### 续行拼接规则

1. 逐物理行读取；以 `YYYY-MM-DD` 开头的物理行 = 一条新逻辑记录的起始行。
2. 不以日期开头的物理行 = 续行，属于上一条记录的第 5 列 `strategy_name`，
   用 `\n` 还原换行拼回（`buffer[4] += "\n" + 续行`）。
3. 每个物理行行尾可能带一个续行标记反斜杠 `\`，读取时去掉。
   只去掉**一个 ASCII 反斜杠**；不能用 `rstrip('\\')`，
   否则 GBK 下尾字节为 `0x5C` 的汉字会被削掉一个字节变成乱码。
4. 起始行按 `\t` 切分：`>=5` 列 → 前 4 列为元数据、第 5 列起拼成 `strategy_name`；
   `==4` 列 → `strategy_name` 为空；`<4` 列 → 损坏行，跳过。
5. 文件开头就出现续行（无上文）→ 丢弃；读完时最后一条记录单独入库。

### 库表与分表

- 单库 `tasks/sqlite_db/loss_control/F09591.db`，128 张表 `data_F09591_0 … data_F09591_127`。
- 路由：`表号 = zlib.crc32(product_no) % NUM_TABLES`，与 `services/data_query.py` 的
  `QUERY_SPECS["loss_control"]`（`single_db_multi_table` 单库多表）一致。
- 每张分表主键 `PRIMARY KEY (product_no)`，入库 `INSERT OR REPLACE`。
- 保留天数、批次大小、轮询间隔见 `.env`（`LOSS_CONTROL_*`）。
- 量级：每个 AVL 约 7000 万逻辑记录；按 `product_no` 覆盖后实际行数取决于去重后的手机号数。
  PRAGMA 用 `synchronous = NORMAL`。

## 业务库读取（`services/data_query.py`）

两个任务原先各有一个 `data_query.py`，逻辑重复、改字段要同步两处。现在合并为
**一个模块 + 两处配置定义**：库目录、文件前缀、分表数来自运行时配置
`sftp_avl2db.config.load_profiles()` 构造的 `TaskProfileConfig`；表列名、SQL 条件与
查询模式来自包内静态契约 `sftp_avl2db/dbspec.py`。`services/data_query.py` 只负责按任务名
派发和执行，`main.py` 一行未改。

### 配置来源（与写入侧共用）

| 配置 | 位置 | 在线读取侧用途 |
|---|---|---|
| `.env` 各任务分组的 `*_FILE_PREFIXES` / `*_NUM_TABLES` / `*_TABLE_NAME_PREFIX`（业务前缀见「任务命名」） | `PROFILES[任务名]`（`TaskProfileConfig`） | 库文件名、分表数、表名前缀；改 `.env` 读写两侧同时生效 |
| `DB_SCHEMAS[任务名]` | `sftp_avl2db/dbspec.py` | `(列名, 类型)` 列表，既是写入侧建表定义，也是 `SELECT *` 结果的 `dict` 组装顺序 |
| `QUERY_SPECS[任务名]` | `sftp_avl2db/dbspec.py` | 查询模式、条件列、排序、业务 ID 映射 |
| `TaskProfileConfig.db_dir` | `sftp_avl2db/config.py` | 固定指向 `tasks/sqlite_db/{任务名}/`，与写入路径一致 |

`PROFILES`（运行时）/ `DB_SCHEMAS` / `QUERY_SPECS`（静态）三者 key 完全一致
（`opportunity_recall` / `loss_control`）。`data_query` 在模块加载时显式调用
`load_profiles()` + `validate_specs(PROFILES)` 校验列与前缀的合法性，漂移即抛
`ValueError`；写入侧 `load_profiles()` 同样会校验。**import 配置模块本身零 I/O**，
不再在模块导入时自动读 `.env` 或校验。

### `mode` 的三种库表形式

`mode` 按「库数量 × 表数量」划分，枚举写在 `sftp_avl2db/dbspec.py` 的 `QUERY_MODES`。
**多库多表场景不存在，不在支持范围内**，配置里出现任何其它值会在
`validate_specs()` 时抛 `ValueError`。

> **这三个值就是唯一的 `mode` 取值**，不再有第二套名字：
> `TaskProfileConfig.mode` 由 `load_profiles()` 从这里派生，写入侧
> `sftp_avl2db/sink/router.py::build_layout()` 按同一组值分派到
> `PerPrefixLayout` / `Crc32ShardLayout` / `FixedTableLayout`。
> `validate_specs()` 会校验 `profile.mode` 与 `QUERY_SPECS[].mode` 一致，
> 手填漂移在启动时就抛 `ValueError`。分表口径统一为
> `zlib.crc32(product_no.encode("utf-8")) % NUM_TABLES`。

| mode | 库表形式 | 库文件 | 表名 | 专用字段 |
|---|---|---|---|---|
| `multi_db_single_table` | 多库单表 | 按 `product_id` 选 `{库名}.db` | 每库一张 `data_{库名}` | `db_map` |
| `single_db_multi_table` | 单库多表 | `{FILE_PREFIXES[0]}.db` | `{TABLE_NAME_PREFIX}_{crc32 % NUM_TABLES}` | 无（用 `.env` 的分表配置） |
| `single_db_single_table` | 单库单表 | `{FILE_PREFIXES[0]}.db` | `table_name` 指定的固定表 | `table_name` |

当前两个任务只用到前两种；`single_db_single_table` 已实现但暂无任务使用，
新增单库单表任务时补 `table_name` 即可。`services/data_query.py::_resolve()` 按
`mode` 走三个分支，各自算出 `(db_path, table_name, where 值列表)`。

> **库名与表名分开**：`db_map` 是**库名映射**（`product_id → 库名`），值是 `.db`
> 文件名主体，不是表名。多库单表模式下表名按固定约定 `data_{库名}` 派生，
> 与写入侧 `init_db()` 建的 `data_{前缀}` 一致。

### `QUERY_SPECS` 字段

| 字段 | 含义 |
|---|---|
| `mode` | 库表形式，必须是 `QUERY_MODES` 三者之一，见上表 |
| `where` | 条件列名数组，值由模块按列名从 `(telnum, product_id)` 取，用参数绑定传入 SQL |
| `order_by` | 排序表达式，`None` 表示不排序；两者都 `LIMIT 1` |
| `db_map` | 多库单表专用：**库名映射** `product_id → 库名`；库文件 `{库名}.db`，表名 `data_{库名}`。库名必须存在于 `FILE_PREFIXES` |
| `table_name` | 单库单表专用：固定表名 |
| `product_ids` | 供 `main.py` 路由判断的业务 ID 列表 |

### 两个任务的解析结果

| 任务名（= `PROFILES` / `QUERY_SPECS` key） | mode | 库文件 | 表名 | 条件与排序 | 返回列 |
|---|---|---|---|---|---|
| `opportunity_recall` | `multi_db_single_table` | `tasks/sqlite_db/opportunity_recall/{库名}.db` | `data_{库名}`，库名来自 `db_map` | `product_no = telnum` + `ORDER BY op_time DESC` | 12 列（op_time…city_id） |
| `loss_control` | `single_db_multi_table` | `tasks/sqlite_db/loss_control/F09591.db` | `data_F09591_{crc32 % NUM_TABLES}` | `product_no = telnum` + `ORDER BY op_time DESC` | 5 列（op_time, user_id, product_no, city_id, strategy_name） |

### 对外接口

| 接口 | 说明 |
|---|---|
| `query(task_name, telnum, product_id=None)` | 通用派发；`task_name` 必须是 `QUERY_SPECS` 的 key |
| `get_opportunity_recall_latest(telnum, product_id)` | 上表两行的薄封装，保持 `main.py` 调用点语义清晰 |
| `get_loss_control_latest(telnum)` | 同上 |
| `OPPORTUNITY_RECALL_PRODUCT_IDS` / `LOSS_CONTROL_PRODUCT_IDS` | 由 `db_map` / `product_ids` 派生的不可变集合，供路由判断 |

均为 `async` 函数、内部同步 `sqlite3`；表名与列名来自代码内配置，用户输入只走参数绑定。

### 加任务或改字段

- 改列名/列顺序 → 只改 `sftp_avl2db.dbspec.DB_SCHEMAS[任务名]`（写入侧建表与读取侧 `dict` 组装同时生效）。
- 改条件或排序 → 只改 `sftp_avl2db.dbspec.QUERY_SPECS[任务名]`。
- 加分表数/换表名前缀/换文件前缀 → 改 `.env` 对应业务前缀分组的键，读写两侧同时生效。
- 换库表形式 → 改 `QUERY_SPECS[任务名]["mode"]` 并补齐对应的专用字段
  （多库单表要 `db_map`、单库单表要 `table_name`、单库多表用 `.env` 分表配置），
  三种形式的分支已经在 `services/data_query.py::_resolve()` 与
  写入侧 `sftp_avl2db/sink/router.py::build_layout()` 里按同一组值实现。
- 新增一个任务 → 在 `sftp_avl2db.dbspec.DB_SCHEMAS` / `QUERY_SPECS` 与
  `sftp_avl2db/config.py` 的 profile 定义各加一项；`mode` 只需在
  `QUERY_SPECS` 里声明一次，`load_profiles()` 会自动派生到 profile，
  `main.py` 用 `query(任务名, ...)` 调用即可，无需新增查询模块。

主键、分表数与库表定义的完整口径见上文「字段」「库表与分表」小节。

## 业务库话术重组（2 个 spec 子类）

| 子类 | 读取字段 | 重组规则 |
|---|---|---|
| `rewrite_by_spec_f09554` | `content_abstract` / `type_xl` / `cust_demand` / `rec_strategy` / `reject_reason` / `reason_note` | 拼【通话总结】【用户需求】【已推荐政策】【拒绝原因】四段，标题加粗、部分蓝色，缺值写「无」；末尾拼 `auxiliary_portrait`，换行统一替换为 `<br>` |
| `rewrite_by_spec_f09591` | `strategy_name` | 自定义标签 `<$B>` / `<$I>` / `<$U>` / `<#RRGGBB>` / `<$B$I#RRGGBB>` → `<b>` / `<i>` / `<u>` / `<span style="color: #RRGGBB">`，`</>` 按栈逆序闭合；空内容返回「无历史话术记录」 |

这些格式化都在业务侧执行，库里保存的是原始文本。
