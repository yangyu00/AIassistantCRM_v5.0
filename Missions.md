# 定时数据更新任务说明（SFTP AVL 拉取 → SQLite 入库）

> 3 个任务各自独立进程运行，代码在 `tasks/`；在线业务（FastAPI）只读 SQLite。
> 原随 `main.py` 启动的拉取实现（`sftp_workdir_*/run.py`、`sftp_service.py`）已删除，
> 三个 `sftp_workdir_*` 目录现只保留在线查询用的 `data_query.py`。

## 通用规则

1. **远程文件**：连接参数见 `.env` 的 `SFTP_*`。
   每天生成 `{业务前缀}{YYYYMMDD}000000.AVL`，制表符分隔，UTF-8。
2. **不重复拉取**：每个任务维护 `tasks/state/{任务名}.fetched`，记录已成功入库的远程文件名；
   每轮先跳过清单里的文件，**入库成功后才登记**。本地已存在同名 `.AVL` 也跳过。
3. **失败重试**：入库失败不登记清单，下一轮自动重试；本地文件保留。
4. **跨天兜底**：每轮同时检查「昨天 + 今天」两天的文件名。
   > ⚠️ **文件名日期 ≠ 数据日期（op_time）**：`{YYYYMMDD}` 是文件生成日，`op_time` 是业务数据日，各任务偏移量见下文。
   > 补跑时 `--date` 填**文件名日期**：补 op_time=2026-11-08 的数据，任务1 用 `--date 20261110`，任务2 用 `--date 20261109`。
5. **单实例锁**：`tasks/state/{任务名}.lock`（portalocker/fcntl 非阻塞锁）。
6. **轮询间隔**：由 `.env` 的 `TASK{1,2,3}_POLL_INTERVAL_SECONDS` 控制（默认 600 / 1800 / 3600 秒）。
7. **本地文件**：`tasks/files/{任务名}/`，按 `.env` 的 `TASK{1,2,3}_LOCAL_FILE_EXPIRE_DAYS` 天清理。
8. **日志**：`tasks/log/{任务名}_INFO|ERROR_日期.log`，每天轮转，保留 7 天。
9. **运行方式**（在项目根目录执行，用 `-m` 保证包内相对导入生效）：
   ```bash
   python -m tasks.task_f09554_opp
   python -m tasks.task_f09591_loss --date 20261110
   python -m tasks.task_f09609_diag --date 20261110 --force-reload
   ```
   生产建议用 systemd/nohup 各起一个常驻进程；单机单实例。

---

## 配置

- 三个任务的配置统一由 `tasks/config.py` 读取并解析项目根目录 `.env`，脚本从 `TASKS[...]` 取值。
- **不沿用项目总的 `config.yaml` / `config.py`**；配置只在 `tasks/config.py` 一处定义。
- 在线查询 `sftp_workdir_*/data_query.py` 复用同一份 `TASKS[...]`，库目录由 `db_dir(任务名)`
  统一指向 `tasks/sqlite_db/{任务名}/`，与三个任务脚本的写入路径一致，读写不再各算一套。
- 优先级：**系统环境变量 > `.env`**；设置 `TASKS_ENV_FILE` 可指向其他 `.env` 路径。
  `.env` 支持 `#` 注释、行内注释、`export`、`${VAR}` 插值、单/双引号；缺必需键会在启动时报错，不会静默连错服务器。
- 分组：SFTP 公共 5 项（`SFTP_HOST/PORT/USERNAME/PASSWORD/REMOTE_PATH`），
  每个任务一组 `TASK{1,2,3}_*`（前缀列表、批次大小、库/本地文件保留天数、轮询间隔），
  任务2/3 另有 `NUM_TABLES`、`TABLE_NAME_PREFIX`。

  默认值如下（表中键名省略前缀 `TASK1_` / `TASK2_` / `TASK3_`，分表键同理）：

  | 键 | 任务1 | 任务2 | 任务3 |
  |---|---|---|---|
  | `FILE_PREFIXES` | F09554,F09555,F09556 | F09591 | F09609 |
  | `BATCH_SIZE` | 10000 | 5000 | 5000 |
  | `DATA_EXPIRE_DAYS` | 30 | **3** | 50 |
  | `LOCAL_FILE_EXPIRE_DAYS` | 7 | **3** | 50 |
  | `POLL_INTERVAL_SECONDS` | 600 | 1800 | 3600 |
  | `NUM_TABLES` / `TABLE_NAME_PREFIX` | — | 128 / `data_F09591` | 128 / `data_F09609` |

  > 任务2 保留天数为 **3 天**；需要 50 天改 `.env` 一行即可。
- `.env` 含 SFTP 明文密码：仓库公开时建议加入 `.gitignore`，或部署时改用系统环境变量注入（不提交 `.env`）。
- 本方案取代原待办「统一迁到 `config.yaml`」：不并入项目总配置，避免 tasks 与在线业务的配置耦合。

---

## 入库幂等

- **任务1、任务2**：`PRIMARY KEY (product_no)` + `INSERT OR REPLACE`，同一 `product_no` 只保留一条。
- **任务3**：`PRIMARY KEY (product_no, product_id)` + `INSERT OR REPLACE`（一个手机号对应多个产品各一条）。
- `product_no` 为 `TEXT NOT NULL`；空手机号记录在解析时跳过，跳过条数写进日志。
- 主键自带索引，不再额外建 `(product_no, op_time)` 索引。
- `REPLACE` 是后写覆盖先写。批量补跑历史文件按文件名日期**升序**执行（先旧后新）；
  常驻轮询按「昨天 → 今天」跑，天然符合。
- 在线查询 `data_query.py` 表名、列名、分表路由键不变，库路径见「配置」小节。

---

# 任务1 回捞商机（号卡 / 套餐 / 宽带）AVL 入库（F09554 / F09555 / F09556）

进程：`tasks/task_f09554_opp.py`；任务名 `f09554_opp`。

## 文件日期与数据日期

**op_time = 文件名中 YYYYMMDD 的前两天（T-2）**，直接取自文件第 1 列，代码不做日期改写。

| 文件名 | 文件日期 | 入库 op_time |
|---|---|---|
| `F0955420261110000000.AVL` | 2026-11-10 | **2026-11-08** |
| `F0955520261110000000.AVL` | 2026-11-10 | **2026-11-08** |
| `F0955620261110000000.AVL` | 2026-11-10 | **2026-11-08** |

## 字段（12 列，一行一条，无续行）

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
- 库表：每个前缀一个库 `tasks/sqlite_db/f09554_opp/{F09554,F09555,F09556}.db`，
  表名 `data_{前缀}`，主键 `PRIMARY KEY (product_no)`；与在线查询
  `PRODUCT_TABLE_MAP`（202608041615335173/172/174）对应。
- 保留天数、批次大小、轮询间隔见 `.env`（`TASK1_*`）。
- 量级：每个 AVL 每天不到 1 万条，单库单表即可；覆盖后每库行数 ≤ 去重后的手机号数。

# 任务2 控损 AVL 入库（F09591）

进程：`tasks/task_f09591_loss.py`；任务名 `f09591_loss`。

## 文件日期与数据日期

**op_time = 文件名中 YYYYMMDD 的前一天（T-1）**，直接取自文件第 1 列，代码不做日期改写。

| 文件名 | 文件日期 | 入库 op_time |
|---|---|---|
| `F0959120261110000000.AVL` | 2026-11-10 | **2026-11-09** |

## 字段（逻辑 5 列）

| # | 字段 | 类型 | 说明 |
|---|---|---|---|
| 1 | op_time | TEXT | 数据日期 = 文件名日期 - 1 天；过期清理按此列 |
| 2 | user_id | TEXT | 用户标识 |
| 3 | product_no | TEXT NOT NULL | 手机号（**主键**、分表键、查询键） |
| 4 | city_id | TEXT | 地市编码 |
| 5 | strategy_name | TEXT | 控损策略正文（**含裸换行，需续行拼接**） |

## 续行拼接规则

1. 逐物理行读取；以 `YYYY-MM-DD` 开头的物理行 = 一条新逻辑记录的起始行。
2. 不以日期开头的物理行 = 续行，属于上一条记录的第 5 列 `strategy_name`，
   用 `\n` 还原换行拼回（`buffer[4] += "\n" + 续行`）。
3. 每个物理行行尾可能带一个续行标记反斜杠 `\`，读取时去掉。
   只去掉**一个 ASCII 反斜杠**；不能用 `rstrip('\\')`，
   否则 GBK 下尾字节为 `0x5C` 的汉字会被削掉一个字节变成乱码。
4. 起始行按 `\t` 切分：`>=5` 列 → 前 4 列为元数据、第 5 列起拼成 `strategy_name`；
   `==4` 列 → `strategy_name` 为空；`<4` 列 → 损坏行，跳过。
5. 文件开头就出现续行（无上文）→ 丢弃；读完时最后一条记录单独入库。

## 库表与分表

- 单库 `tasks/sqlite_db/f09591_loss/F09591.db`，128 张表 `data_F09591_0 … data_F09591_127`。
- 路由：`表号 = zlib.crc32(product_no) % 128`，与在线查询 `data_query.get_latest_record` 一致。
- 每张分表主键 `PRIMARY KEY (product_no)`，入库 `INSERT OR REPLACE`。
- 保留天数、批次大小、轮询间隔见 `.env`（`TASK2_*`）。
- 量级：每个 AVL 约 7000 万逻辑记录；按 `product_no` 覆盖后实际行数取决于去重后的手机号数。
  PRAGMA 用 `synchronous = NORMAL`。

# 任务3 诊断 AVL 入库（F09609）

进程：`tasks/task_f09609_diag.py`；任务名 `f09609_diag`。

## 文件日期与数据日期（待业务确认）

op_time 直接取自文件第 1 列，代码不做日期改写。当前样例 `F0960920261110000000.AVL` 内的 op_time 是 **2026-11-08**，比文件名日期早 2 天（T-2）。

## 字段（宽表展开后 5 列）

| # | 字段 | 类型 | 说明 |
|---|---|---|---|
| 1 | op_time | TEXT | 数据日期 = 文件第 1 列原值；过期清理按此列 |
| 2 | user_id | TEXT | 用户标识 |
| 3 | product_no | TEXT NOT NULL | 手机号（分表键、查询键之一） |
| 4 | product_id | TEXT NOT NULL | 产品 ID（与 product_no 联合主键） |
| 5 | strategy_script | TEXT | 该产品对应的策略/话术（**含裸换行**） |

## 续行与展开规则

1. 以「行首是否 `YYYY-MM-DD`」区分新记录起始行与续行，续行合并进当前逻辑记录。
2. 一条逻辑记录的所有物理行先收集齐，用 `\n` **join 成整段文本后统一按 `\t` 切列**
   （即使 tab 被换行拆开也能正确切分）。
3. 切列后第 1~3 列 = `op_time / user_id / product_no`；
   **第 4 列起每两列一组 `(product_id, strategy_script)`**，每组生成一条入库记录。
4. `product_id` 为空（纯空白）的组跳过；总列数 `< 3` 的逻辑记录整条跳过并告警。
5. 行尾反斜杠处理同任务2（只剥一个 ASCII 反斜杠）。

## 库表与分表

- 单库 `tasks/sqlite_db/f09609_diag/F09609.db`，128 张表 `data_F09609_0 … data_F09609_127`。
- 路由同任务2：`zlib.crc32(product_no) % 128`，与 `data_query.get_record` 一致。
- 主键 `PRIMARY KEY (product_no, product_id)`，入库 `INSERT OR REPLACE`。
- 保留天数、批次大小、轮询间隔见 `.env`（`TASK3_*`）。
- 量级：拆分后每天约 3 亿条 → 每表日均约 234 万条；`synchronous = NORMAL`。
  若单机入库速度不够，可增大 `.env` 的 `TASK3_BATCH_SIZE` 或调低 `TASK3_POLL_INTERVAL_SECONDS`。

---

## 补充/待确认事项

- **重复模块已清理**：数据更新统一由 `tasks/` 独立进程承担，`main.py` 不再保留旧的
  startup 线程启动入口；配置与库路径详见「配置」小节。
- 样本文件 `io_samples/*.AVL` 中：F09591/F09609 每条逻辑记录有 2 条物理记录、多组产品时列数为 9（3 + 3 组）；
  生产上 F09609 约 103 列（约 50 组），解析逻辑与列数无关。
- **op_time 偏移量**：任务1 = T-2、任务2 = T-1（已确认）；任务3 按样例推断为 T-2，待生产数据核实。
  过期清理比较的是 op_time，因此保留天数实际覆盖的文件日期要多出对应的偏移天数。

---

## 维表 SQLite 化（第一阶段：写入侧）

> 现状：`main.py` startup 定时比对 `excel2consts/*.xlsx` 的 mtime，改写 `src/consts.py` 再 reload 热更新。
> 目标：改为写本地 SQLite，用 `version` 字段维护版本，在线业务按版本读取，摆脱「改源码 + reload」。
> **本阶段只做第一步**：独立进程检测注册文件是否更新 → 按配置抽列写库。
> `main.py`、`src/consts.py`、`services/`、在线业务读取逻辑**一律未改**，旧热更新链路仍原样运行。
> 假设维表 xlsx 仍放在 `excel2consts/` 目录下（仓库未提交 xlsx，仅有 `io_samples/` 下的同名样例）。

### 新增文件（均在 `excel2consts/`，未改动项目其余文件）

| 文件 | 作用 |
|---|---|
| `registry.json` | 注册表：库路径、轮询间隔、文件↔表名↔主键↔字段映射 |
| `dim_config.py` | 加载并校验 `registry.json`，相对路径解析到 `excel2consts/` |
| `dim_importer.py` | 读 xlsx 抽列、建表、指纹比对、整表替换与 version 递增 |
| `dim_watcher.py` | 独立进程主程序：单实例锁、定时轮询、日志、参数解析 |
| `__init__.py` | 使 `python -m excel2consts.dim_watcher` 可用 |

运行时产物也在 `excel2consts/` 下：`sqlite_db/`、`state/dim_watcher.lock`、`log/dim_watcher.log`。

### 运行方式（项目根目录）

```bash
python -m excel2consts.dim_watcher                 # 常驻轮询，默认 60s
python -m excel2consts.dim_watcher --once         # 只检测一轮后退出（便于 crontab / 验证）
python -m excel2consts.dim_watcher --once --force # 忽略指纹强制重导并递增 version
python -m excel2consts.dim_watcher --config /path/to/registry.json --interval 300
```

生产建议 systemd/nohup 起一个常驻进程，与 `tasks/` 三个任务并列；单机单实例，靠
`state/dim_watcher.lock`（`fcntl` 非阻塞锁）互斥。`--force` 通常只配合 `--once` 使用。

### 可配置项（`registry.json`）

| 配置 | 含义 |
|---|---|
| `database` | SQLite 库路径，默认 `excel2consts/sqlite_db/dimensions.db` |
| `poll_interval_seconds` | 轮询间隔（秒），可被 `--interval` 覆盖 |
| `version_column` | 版本列名，默认 `version` |
| `meta_table` | 版本元数据表名，默认 `dim_meta` |
| `tables[].source` | 注册文件名 → 对应**英文表名** `tables[].table` |
| `tables[].sheet` / `header_row` | 工作表（序号或名称）、表头行号（1-based） |
| `tables[].primary_key` | 主键列（数组，支持联合主键），必须来自 `columns[].column` |
| `columns[].source` → `columns[].column` | **Excel 表头 → SQLite 列名**的映射 |
| `columns[].column_index` | 可选，按列序号（1-based）取列；同名表头（如卖点维表有两个「备注」）时更稳妥。与 `source` 同时给出时优先用序号 |
| `columns[].type` | `TEXT` / `INTEGER` / `REAL`，默认 `TEXT`（与旧逻辑一致，全部按字符串读） |
| `columns[].default` / `strip` / `optional` | 空值兜底、是否去首尾空白、缺列时是否允许跳过 |

当前注册两张表（列名沿用 `src/consts.py` 中变量名，便于后续切换）：

| 注册文件 | 表名 | 主键 | 抽取字段（Excel 表头 → 列名） |
|---|---|---|---|
| `卖点维表.xlsx` | `product_selling_point_match` | `business_id` | 业务ID→business_id、产品名称→product_name、生成话术→selling_point、标签ID→label_id、兜底话术→fallback_script、匹配结果2→match_result、产品大类→product_category、产品小类→product_subcategory |
| `标签转换维表.xlsx` | `label_name_mapping` | `label_id` | 标签ID→label_id、营销助手显示名字→display_name、标签管理平台标签名→platform_label_name |

**加字段、改主键、加表只需改 `registry.json`，不用改 Python 代码。**
配置加载时会校验表名/列名合法性、主键必须来自列定义且不能重复或设为 optional、
列名不与版本列冲突、表名不重复；
已有表的结构与配置不一致会直接报错并提示删库重建，不会静默写错列。

### 表结构与 version 语义

- 每张表 = 配置列 + `version INTEGER NOT NULL` + `PRIMARY KEY (主键列)`；主键列额外 `NOT NULL`。
- 每次检测到文件变更：**单事务内 `DELETE` 整表 → 写入全量行（每行带同一个新 version）→ 更新元数据**，
  对应旧逻辑「合成 dict 后整体替换」，不会残留已删除的行。
- `version` 从 1 起，每次成功导入 +1；`dim_meta` 记录每个表的
  `version / source_file / sha256 / file_size / file_mtime / row_count / updated_at`。
  数据表只保留**当前版本**，历史版本暂不落库（后续如需回溯，可改为按 version 追加）。
- 主键为空的行跳过，重复主键后写覆盖先写（与旧 `dict(zip(keys, values))` 语义一致），计数写进日志。

### 变更检测

- 用 **sha256 内容指纹**判断，不只比 mtime（避免「另存为但 mtime 相同」或「touch 一下」误判）。
  指纹未变 → 直接 SKIP，version 不变。
- 指纹存 `dim_meta`，进程重启不会重复导入；**导入失败不更新指纹**，下一轮自动重试。
- 注册文件不存在 → 告警跳过，不退出进程（维表可能稍后才放进来）。
- 缺必需表头列 → 报错跳过本轮、不写库、不更新指纹，下轮重试；`optional: true` 的列缺失时填空串。
- xlsx 正在被 Excel 写入导致读取失败时同样按失败处理，下轮重试。

### 本阶段未做（避免牵动在线业务）

- **不动 `main.py` / `src/consts.py`**：在线业务仍读 `consts` 里的 dict，旧 startup 热更新照常跑，
  两套链路目前并存，本进程只负责把维表数据写进 SQLite。
- 未实现旧脚本里的复杂话术加工（`cut_in_str2tuple` 解析切入/挽留话术、`匹配结果2` 分组、
  动态列 `标签0…标签26`）。本阶段只做固定表头的**列映射直取**；需要加工时再以可配置的
  transform 规则扩展，避免一上来引入难以排查的转换 bug。
- 未做在线业务按 version 读取、未做版本回滚/历史版本查询、未接入 `tasks/config.py` 的 `.env`
  （本模块只依赖 `registry.json`，自包含）。

### 依赖与注意事项

- 只需 `openpyxl`（读 xlsx），不引入 pandas / yapf；标准库完成 SQLite、指纹、日志与文件锁。
- `fcntl` 文件锁为 Linux 单机单实例设计，与项目部署环境一致。
- 表结构变更（增删列、改主键）需人工删除 `excel2consts/sqlite_db/dimensions.db` 后重建，
  本阶段不做表结构迁移。
- 试跑：仓库 `io_samples/` 下有 `卖点维表.xlsx` / `标签转换维表.xlsx` 样例，
  复制到 `excel2consts/` 后执行 `python -m excel2consts.dim_watcher --once` 即可。

