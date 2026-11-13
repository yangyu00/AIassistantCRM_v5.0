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
- 未做基于 `version` 的缓存/灰度切换、未做版本回滚/历史版本查询、未接入 `tasks/config.py` 的 `.env`
  （本模块只依赖 `registry.json`，自包含）。在线业务读取侧的进展见下一节。

### 依赖与注意事项

- 只需 `openpyxl`（读 xlsx），不引入 pandas / yapf；标准库完成 SQLite、指纹、日志与文件锁。
- `fcntl` 文件锁为 Linux 单机单实例设计，与项目部署环境一致。
- 表结构变更（增删列、改主键）需人工删除 `excel2consts/sqlite_db/dimensions.db` 后重建，
  本阶段不做表结构迁移。
- 试跑：仓库 `io_samples/` 下有 `卖点维表.xlsx` / `标签转换维表.xlsx` 样例，
  复制到 `excel2consts/` 后执行 `python -m excel2consts.dim_watcher --once` 即可。

---

## 维表 SQLite 化（第二阶段·第 1 步：替换 label_name_mapping 读取）

> 逐步替换在线业务对 `src/consts.py` 的依赖。**本步只换一处**：
> `services/product_info_raw.py` 里辅助画像拼装时对 `consts.label_name_mapping` 的查找。
> 其余 `consts` 变量（`product_selling_point_match_dict`、`cut_in_script_dict`）
> 及所有业务判断逻辑一律未动。

### 改动文件

| 文件 | 改动 |
|---|---|
| `services/product_info_raw.py` | 原两行 `in consts.label_name_mapping.keys()` / `consts.label_name_mapping[...]` 改为查库得到的 `label_name_mapping` 局部变量；`from src import consts` 保留（另有 3 处仍用） |
| `excel2consts/dim_reader.py`（新增） | 在线业务只读查询侧，提供 `load_lookup(table_name) -> dict` |
| `excel2consts/registry.json` | `label_name_mapping` 表新增 `lookup: {"key": "label_id", "value": "display_name"}` |
| `excel2consts/dim_config.py` | 校验并透传 `lookup`（key/value 必须来自 `columns` 中已声明的列） |
| `Missions.md` | 追加本节 |

`dim_watcher.py` / `dim_importer.py` 未改动，写入侧行为不变。

### 替换前后

```python
# 替换前
if self.queried_label_ids[i] in consts.label_name_mapping.keys():
    label_name = consts.label_name_mapping[self.queried_label_ids[i]]

# 替换后（循环外先加载一次）
label_name_mapping = load_lookup("label_name_mapping")
...
if self.queried_label_ids[i] in label_name_mapping:
    label_name = label_name_mapping[self.queried_label_ids[i]]
```

`label_name_mapping` 在 `__get_auxiliary_portrait` 循环**外**加载一次，
每个 `ProductInfoRaw` 实例只开一次连接，循环内仍是纯内存 dict 查找。
判断条件、后续所有标签特殊处理规则、输出字符串格式**逐字未改**。

### 读取侧约定

- 库路径、`table_name`、key/value 列名全部取自 `registry.json` 的 `database` / `tables[].lookup`，
  不在业务代码里硬编码；换列名、换库路径只改配置。
- **只读打开**：`sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)`。
  在线业务绝不创建空库、绝不写入维表库，避免与 `dim_watcher` 抢写。
- **优雅降级**：库文件不存在、表不存在、未声明 `lookup`、查询异常、
  `registry.json` 缺失或损坏 → 返回空 `{}`，业务行为退化为「标签名沿用上游
  `custom_label_info` 给的名字」，与旧逻辑「key 不在 dict 里时不改名」完全一致。
  即维表侧出任何问题都不会让 API 500。同一类告警每进程只打印一次，避免刷屏。
- `dim_watcher` 尚未启动 / 维表还没导入时，在线业务不会报错，只是没有映射生效。
- **不做进程级缓存**：每次实例读一次全表（当前 60 行），天然读到 `dim_watcher` 写入的最新版本，
  没有缓存失效、脏读、reload 时序问题。`version` 字段目前只记在 `dim_meta` 供审计，
  后续若要省连接可再按 `table_name` 的 version 做缓存——注意各表 version 独立。

### 两套链路并存的现状

- `main.py` startup 仍会定时调用 `excel2consts/labelName1_to_labelName2.py` 重写
  `src/consts.py` 并 `importlib.reload(consts)`；这一步**没动**。
- 但 `consts.label_name_mapping` 现在**已无人读取**，变成死数据；旧热更新是否跑、
  跑几次都不再影响辅助画像的标签名。`product_selling_point_match_dict` 已在第 2 步迁完；
  等 `cut_in_script_dict` 也迁完，再整体拆掉 startup 里的旧链路，避免中途出现半新半旧。
- 新的生效路径：`dim_watcher` 独立进程 → `excel2consts/sqlite_db/dimensions.db`
  → `load_lookup()` → `ProductInfoRaw`。**部署时两个进程都要起**。

### 本步未做

- 本步未替换 `product_selling_point_match_dict`、`cut_in_script_dict`；前者已在
  第 2 步完成，后者仍留在 `consts`（`rewrite_by_excel/rewrite.py` 多处引用）。
- 本步未删除 `excel2consts/labelName1_to_labelName2.py` 等旧转换脚本，未改 `main.py`
  （第 2 步为替换路由判断才改了 `main.py`）。
- 未实现话术加工（`cut_in_str2tuple`、动态 `标签0…标签26` 分组），库里仍存原始列值。
- 未做版本回滚、历史版本查询、基于 version 的灰度切换。

### 验证要点

- 用 `io_samples/标签转换维表.xlsx` 跑 `dim_watcher --once` 建库后，
  同一组 `custom_label_info` 下 `auxiliary_portrait` 与旧 `consts` 版**逐字一致**；
  样例里的真实标签 `bfa9a99b…` 显示名「常去商超」能被在线业务直接读到。
- 删库 / 表不存在 / `registry.json` 损坏时退化为无映射版，也与旧逻辑一致、不抛异常。
- `dim_watcher` 更新文件使 version 递增后，在线业务**无需 reload 任何模块**，
  下一次请求即读到新名字；反过来改 `consts.label_name_mapping` 已完全不影响结果。
- `mode=ro` 验证：删库后调用 `load_lookup` 不会创建任何文件。
- `import main` 正常，其余 services 模块行为不变；改动仅限本节列出的 5 个文件。

---

## 维表 SQLite 化（第二阶段·第 2 步：替换 product_selling_point_match_dict）

> 本步把卖点维表相关的读取全部改为查库。**`cut_in_script_dict` 仍留在 `consts`**，
> 留待第 3 步；旧转换脚本与 `main.py` startup 热更新链路仍未删除。

### 字段口径核对（结论：判断基本正确，两处需精确化）

| 判断 | 核对结果 |
|---|---|
| 用到 `business_id` / `product_name` / `selling_point` / `label_id` | ✅ 对。`business_id` 是**主键/查找键**，其余 3 列组成 value 三元组 `(product_name, selling_point, label_id)`，对应旧 dict 的 `[0]` `[1]` `[2]` |
| `selling_point` 涉及添加 HTML 标签的格式转换 | ✅ 确实存在，但**属于业务侧展示逻辑，不在写库侧**。见下文 |
| `label_id` 根据换行拆分成列表 | ✅ 对，但**拆分应继续留在业务侧**，读取侧返回原始含换行字符串。见下文 |

另外补一处容易漏的：`main.py` 里 `in consts.product_selling_point_match_dict.keys()`
是**「维表中业务」路由判断**，本步也一并改为查库。

### 关于 HTML 格式转换（重要）

转换代码在 `services/rewrite_by_excel/rewrite.py::__get_selling_point()`：
用正则 `\d、.*?：` 找「1、资费：」「2、价格：」等编号段，把首段包进
`<strong>` + `<span style="color:red;">`，后续段落逐段 `<strong>` 拼接并补换行；
匹配不到编号时只加 `【卖点】\n` 前缀。输出再经 `__strong_text()` 把
`【辅助画像】/【切入话术】/【卖点】/【挽留话术】` 包成 `<strong>`。

**本步的处理：库里继续存原始「生成话术」文本，转换逻辑一行未改，只换数据源。**
理由：

- 这是纯字符串 → HTML 的**运行时展示格式化**，不是维表数据本身的属性；
- 若把格式化后的 HTML 写进库，等于把前端样式固化进数据层，以后改配色/标签要重导全表；
- 保持业务侧原样，才能做到新旧输出**逐字一致**，也符合「逐步替换、不出难查 bug」的前提。

⚠️ 别和另一处混淆：`excel2consts/product_selling_point_match.py::cut_in_str2tuple()`
对**兜底话术**做的 `<strong><span style="color: blue;">` 包装属于
`cut_in_script_dict`，是第 3 步的事，本步不涉及。

### 关于 label_id 换行拆分

样例里 `标签ID` 确实是多行单元格，如
`026de6be…\n71527bf3…`。旧逻辑是 `[2].split("\n")`，本步**原样保留**这一行：

```python
return self.product_selling_point_match_dict[self.product_id][2].split("\n")
```

读取侧只返回 `label_id` 原始字符串（库里已按 `strip()` 存，与旧生成器
`label_id.strip()` 一致），不提前拆、不过滤空串、不额外 strip 每一项。
连续换行产生的空串旧逻辑不过滤，新逻辑也不过滤，保证 `queried_label_ids`
逐字一致——否则会静默改变 `__get_queried_label_nvs` 与辅助画像。

### 配置扩展：lookup 支持单值 / 多值

`registry.json` 的 `product_selling_point_match` 新增：

```json
"lookup": {
  "key": "business_id",
  "values": ["product_name", "selling_point", "label_id"]
}
```

`dim_config.py` 的 `lookup` 校验扩展为两种形状，互斥且必填其一：

| 声明 | 返回形状 | 用途 |
|---|---|---|
| `"value": "display_name"` | `{key: 单值}` | `label_name_mapping`（第 1 步，不变） |
| `"values": [列1, 列2, …]` | `{key: (值1, 值2, …)}` | `product_selling_point_match`（本步） |

`values` 的顺序即元组顺序，也就是旧 dict 的 `[0]/[1]/[2]`。
校验包括：列名合法、必须来自 `columns`、不等于 key、不可重复。
**换列名或调顺序只改 `registry.json`，Python 代码不动。**

`dim_reader.py` 的 `load_lookup()` 统一支持两种形状，并新增语义化包装
`load_product_selling_point()`；返回 dict 与旧 `consts.product_selling_point_match_dict`
**完全同形**，所以业务侧 `[0]` / `[1]` / `[2].split("\n")` 一行都不用改。

### 改动文件

| 文件 | 改动 |
|---|---|
| `services/product_info_raw.py` | `__init__` 里加载一次并存为 `self.product_selling_point_match_dict`；`__get_product_name` / `__get_queried_label_ids` 换数据源；删掉已无用的 `from src import consts` |
| `services/rewrite_by_excel/rewrite.py` | `__get_selling_point` 两处换为 `self.product_selling_point_match_dict`；**HTML 转换逻辑逐字未改**；`consts` import 保留（`cut_in_script_dict` 仍在用） |
| `main.py` | 引入 `load_product_selling_point()`；在 `marketAssistant` 循环**外**加载一次供路由判断复用；`from src import consts` 保留（startup 仍需 `importlib.reload(consts)`） |
| `excel2consts/registry.json` | 卖点表新增多值 `lookup` |
| `excel2consts/dim_config.py` | 扩展 `lookup` 校验 |
| `excel2consts/dim_reader.py` | 支持单值/多值，新增 `load_product_selling_point()` |
| `Missions.md` | 追加本节、修正第 1 步的「后续待迁」措辞 |

`dim_watcher.py` / `dim_importer.py` 未改动，写入侧行为不变。

### 读取次数与降级

- **不做进程级缓存**（沿用第 1 步约定）：每次 `load_product_selling_point()` 读一次全表
  （当前 1361 行），天然读到 `dim_watcher` 写入的最新版本，无缓存失效或脏读。
  一次请求最多 2 次全表读：`main.py` 路由判断 1 次 + 每个 `ProductInfoRaw` 实例 1 次；
  `ProductInfoByExcel` 通过 `self` 复用父类那份，不再重复读。本地 SQLite 开销可忽略，
  后续若要省连接可基于**各表独立的 version** 加缓存。
- 降级语义与第 1 步一致：库/表不存在、未声明 lookup、查询异常、`registry.json` 损坏
  → 返回 `{}`。此时 `product_name` 为 `None`、`queried_label_ids` 为 `[]`、卖点为空，
  与旧「key 不在 dict 里」完全一致，**不会让 API 500**。
- 仍为 `mode=ro` 只读，在线业务不创建/不写维表库。

### 与旧 consts 的行为差异（已确认无业务影响）

旧生成器对 `业务ID` 为空的行会生成 `line{i+2}` 这样的占位 key；新库导入时
`dim_importer` 按主键为空直接跳过（样例中 187 行）。这些占位 key 不对应任何真实
`product_id`，业务请求不会命中，故不影响结果。
另外旧生成器用 `dict(zip(...))` 让重复业务 ID 后者覆盖前者，新库
`PRIMARY KEY (business_id)` + 整表替换同样是后写覆盖（样例中 98 行重复），语义一致。

### 两套链路并存的现状

- `consts.product_selling_point_match_dict` 与 `consts.label_name_mapping` **均已无人读取**，
  成为死数据；`consts.cut_in_script_dict` 仍被 `rewrite_by_excel/rewrite.py` 读取。
- `main.py` startup 仍定时重写 `src/consts.py` 并 `importlib.reload(consts)`，本步未动。
  等第 3 步把 `cut_in_script_dict` 迁完，再整体删除旧脚本与 startup 逻辑。
- 生效路径：`dim_watcher` → `sqlite_db/dimensions.db` → `load_product_selling_point()`
  → `ProductInfoRaw` / `ProductInfoByExcel` / `main.py` 路由。**部署时两个进程都要起。**

### 本步未做

- 未替换 `cut_in_script_dict`（含 `cut_in_str2tuple` 话术加工、动态 `标签0…标签26` 分组）。
- 未删除旧转换脚本、未改 startup 热更新、未删 `src/consts.py` 中的变量。
- 未做版本回滚、历史版本查询、基于 version 的缓存或灰度切换。

### 验证要点

- 用 `io_samples/卖点维表.xlsx` 跑 `dim_watcher --once` 建库，按旧生成器逻辑复刻
  `consts` 版 dict 作基准，对多个真实 `business_id` 逐字比对
  `product_name` / `queried_label_ids` / `selling_point`（含 HTML）/ `output_script`。
- 覆盖多段编号话术、无编号话术、`label_id` 多行、以及不在维表中的 `product_id`。
- `consts.product_selling_point_match_dict` 改成任意值后结果不变，确认已成死数据。
- 维表 version 递增后在线业务**无需 reload** 即生效；库不存在时降级且 `import main` 正常。



