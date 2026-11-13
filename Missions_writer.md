# 定时数据更新任务说明（SFTP AVL 拉取 → SQLite 入库）

> 3 个任务各自独立进程运行，代码在 `tasks/`；在线业务（FastAPI）只读 SQLite。
> 三个 `sftp_workdir_*` 目录现只保留在线查询用的 `data_query.py`。

> 在线业务（FastAPI）的读取与重组逻辑见 `Missions_reader.md`。

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

# 维表写入侧（Excel → SQLite）

> 维表由独立进程 `excel2consts.dim_watcher` 从 Excel 抽列写入本地 SQLite。
> **库里只存原始单元格文本**，所有字符串加工（HTML 包装、按换行拆分、分组对齐）
> 都在读取侧完成，不写回库、不固化样式；读取与重组见 `Missions_reader.md`。

## 架构与进程

```
卖点维表.xlsx ─┐
                ├─→ dim_watcher（独立进程，写）─→ excel2consts/sqlite_db/dimensions.db
标签转换维表.xlsx ┘                                      │
                                                        │ mode=ro
                                                        ↓
                                    main.py / services/（在线业务，只读）
```

| 进程 | 命令（项目根目录） | 职责 |
|---|---|---|
| SFTP 拉取 ×3 | `python -m tasks.task_xxx` | AVL → `tasks/sqlite_db/` |
| 维表写入 ×1 | `python -m excel2consts.dim_watcher` | xlsx → `dimensions.db` |
| 在线 API ×1 | `python main.py` | 只读所有 SQLite，无写库逻辑 |

**部署时 5 个进程都要起**。`main.py` 不含任何 startup 定时任务，也不写任何库；
`dim_watcher` 不依赖 `.env` / `tasks/config.py` / 项目总 `config.yaml`，自包含于 `excel2consts/`。

## 文件清单（写入侧）

| 文件 | 作用 |
|---|---|
| `registry.json` | 注册表：库路径、轮询间隔、文件↔表名↔主键↔列映射、动态列、读取侧角色声明 |
| `dim_config.py` | 加载并校验 `registry.json`，相对路径解析到 `excel2consts/`，展开动态列 |
| `dim_importer.py` | 读 xlsx 抽列、建表/结构校验、sha256 指纹比对、单事务整表覆盖与 version 递增 |
| `dim_watcher.py` | 写入侧进程：单实例锁、定时轮询、日志、命令行参数 |
| `__init__.py` | 使 `python -m excel2consts.dim_watcher` 可用 |

读取侧用到的 `dim_reader.py` / `dim_transform.py` 与 `services/` 见 `Missions_reader.md`。

运行时产物（均在 `excel2consts/` 下）：

| 路径 | 内容 |
|---|---|
| `sqlite_db/dimensions.db` | 唯一的维表库 |
| `state/dim_watcher.lock` | `fcntl` 非阻塞锁，保证单机单实例 |
| `log/dim_watcher.log` | 按天轮转（`midnight`），保留 7 天，同时输出到 stderr |

注册文件 `卖点维表.xlsx` / `标签转换维表.xlsx` 放在 `excel2consts/` 目录；
仓库未提交 xlsx，`io_samples/` 下有同名样例。

## 注册配置（`registry.json`）

### 顶层

| 配置 | 当前值 | 含义 |
|---|---|---|
| `database` | `sqlite_db/dimensions.db` | 库路径，相对路径解析到 `excel2consts/` |
| `poll_interval_seconds` | `600` | 轮询间隔（秒），可被 `--interval` 覆盖 |
| `version_column` | `version` | 版本列名 |
| `meta_table` | `dim_meta` | 版本元数据表名 |

### `tables[]`

| 配置 | 含义 |
|---|---|
| `table` | 英文表名 |
| `source` | 注册文件名（相对 `excel2consts/`） |
| `sheet` / `header_row` | 工作表（序号或名称）、表头行号（1-based） |
| `primary_key` | 主键列数组，支持联合主键；必须来自 `columns[].column`，不可重复、不可设为 `optional` |
| `columns[].source` → `columns[].column` | **Excel 表头 → SQLite 列名**映射 |
| `columns[].column_index` | 可选，按列序号（1-based）取列；同名表头时更稳妥。与 `source` 同时给出时优先用序号 |
| `columns[].type` | `TEXT` / `INTEGER` / `REAL`，默认 `TEXT`，全部按字符串读取 |
| `columns[].default` / `strip` / `optional` | 空值兜底、是否去首尾空白、缺列时是否允许填空串 |
| `dynamic_columns` | 把编号表头批量展开为普通列，见下 |
| `lookup` | 读取侧映射声明，见下 |
| `cut_in_script` | 读取侧加工所需的列角色声明，见下 |

### `dynamic_columns`

| 配置 | 当前值 | 含义 |
|---|---|---|
| `source_prefix` | `标签` | Excel 表头前缀 |
| `column_prefix` | `tag_` | SQLite 列名前缀 |
| `start` | `0` | 起始编号（0..10000） |
| `count` | `28` | 数量（1..200） |

展开结果：`标签0 → tag_0` … `标签27 → tag_27`，均为 `optional: true`。
表头缺列时填空串，正好对应读取侧「标签列无内容 → 该分组填 `""`」。
生产维表标签列更多时只改 `count`。

### `lookup`（两种互斥形状，由读取侧消费）

| 声明 | 返回形状 | 用途 |
|---|---|---|
| `"value": "display_name"` | `{key: 单值}` | `label_name_mapping` |
| `"values": ["product_name", "selling_point", "label_id"]` | `{key: (值1, 值2, 值3)}` | `product_selling_point_match` |

`values` 的顺序即元组顺序，业务侧按 `[0] / [1] / [2]` 取值。
`key` / `value` / `values` 必须来自 `columns` 中已声明的列，且不等于 `key`；
`values` 不可重复。换列名或调顺序只改 `registry.json`。

### `cut_in_script`（由读取侧消费）

| 配置 | 当前值 | 含义 |
|---|---|---|
| `key` | `business_id` | 业务 ID 列（查找键） |
| `fallback` | `fallback_script` | 兜底话术原始列 |
| `groups` | `match_result` | 分组 ID 原始列（「匹配结果2」，按 `\n` 拆分） |
| `tags` | `"dynamic_columns"` | 标签话术列的有序数组，或直接引用展开结果；下标 g 对应 `标签{g}` |
| `fallback_key` | `兜底话术` | 生成的嵌套 dict 里兜底项的键名 |

⚠️ `fallback_key` 可配，但 `services/rewrite_by_excel/rewrite.py` 用 `"兜底话术"`
做比较，改它需同步改业务代码。所有角色列必须来自 `columns` 声明。

### 当前注册的两张表

| 注册文件 | 表名 | 主键 | 固定列（Excel 表头 → 列名） | 动态列 |
|---|---|---|---|---|
| `卖点维表.xlsx` | `product_selling_point_match` | `business_id` | 业务ID→`business_id`、产品名称→`product_name`、生成话术→`selling_point`、标签ID→`label_id`、兜底话术→`fallback_script`、匹配结果2→`match_result`、产品大类→`product_category`、产品小类→`product_subcategory` | `标签0…标签27` → `tag_0…tag_27`（28 列） |
| `标签转换维表.xlsx` | `label_name_mapping` | `label_id` | 标签ID→`label_id`、营销助手显示名字→`display_name`、标签管理平台标签名→`platform_label_name` | 无 |

### 配置校验（`dim_config.py`）

启动时校验：表名/列名/版本列名合法性、表名不重复、列名不重复且不与版本列冲突、
主键必须来自列定义、动态列不与已有列冲突、`lookup` 的 `value`/`values` 互斥且必填其一、
`cut_in_script` 各角色列已声明、`tags` 不可重复、`count` 在 1..200、轮询间隔为正整数。
不合法直接抛 `ValueError`，不会静默按错误配置连库或写错列。

## 库表结构

每张表 = 配置列 + `version INTEGER NOT NULL` + `PRIMARY KEY (主键列)`；主键列额外 `NOT NULL`。

| 表 | 列数 | 说明 |
|---|---|---|
| `dim_meta` | 8 | `table_name TEXT PRIMARY KEY, version, source_file, sha256, file_size, file_mtime, row_count, updated_at` |
| `product_selling_point_match` | 37 | 8 个固定列 + 28 个 `tag_*` 列 + `version`；主键 `business_id` |
| `label_name_mapping` | 4 | `label_id, display_name, platform_label_name, version`；主键 `label_id` |

`version` 列写在每一行上，值等于本次导入的版本号。数据表只保留**当前版本**，
被删除的行不会残留；历史版本不落库。

`ensure_table()` 每次启动都会比对 `PRAGMA table_info` 与配置的列集合，
不一致抛 `RuntimeError` 并提示删库重建，不会静默写错列。

## 写入侧流程（`dim_watcher` + `dim_importer`）

### 运行方式

```bash
python -m excel2consts.dim_watcher                 # 常驻轮询，间隔由 registry 控制（当前 600s）
python -m excel2consts.dim_watcher --once         # 只检测一轮后退出（crontab / 验证）
python -m excel2consts.dim_watcher --once --force # 忽略指纹强制重导并递增 version
python -m excel2consts.dim_watcher --config /path/to/registry.json --interval 300
```

生产建议 systemd/nohup 起一个常驻进程，与 `tasks/` 三个任务并列。
`--force` 通常只配合 `--once` 使用。

### 单轮流程（逐表执行）

1. **文件存在性**：注册文件不存在 → 告警跳过该表，不退出进程。
2. **指纹比对**：算文件 sha256，与 `dim_meta` 中该表的 `sha256` 比较；
   相同且非 `--force` → SKIP，version 不变。用内容指纹而非 mtime，
   避免「另存为但 mtime 相同」漏判或「touch 一下」误判。
3. **抽列**：`openpyxl` 以 `read_only=True, data_only=True` 打开，按表头名或
   `column_index` 定位列；`strip` 去首尾空白；`optional` 列缺失时填 `default`（默认空串）；
   缺必需表头列抛 `MissingColumn`，本轮跳过、指纹不更新、下轮重试。
4. **去重**：主键为空的行跳过；重复主键**后写覆盖先写**（与 `INSERT OR REPLACE` 一致）。
   跳过条数、覆盖条数写进日志。
5. **单事务整表覆盖**：`DELETE FROM 表` → 写入全量行（每行带新 `version`）→
   更新 `dim_meta` 该行。事务内任一环节失败整体回滚，指纹不更新，下轮自动重试。
6. **日志**：`[表名] version=N 写入 X 行 (空主键跳过 …，重复主键覆盖 …，sha256=…)`。

其他失败场景：xlsx 正被 Excel 写入导致读取失败、表结构与配置不一致，
同样按「本轮失败 + 下轮重试」处理，不更新指纹、不写坏数据。

指纹存 `dim_meta`，进程重启不会重复导入；**导入失败不更新指纹**，下一轮自动重试。

### version 语义（按表独立，不共享全局计数器）

`dim_meta` 以 `table_name` 为主键，一行对应一张表；每张表各自从 1 起，
**仅在该表对应的文件成功导入时 +1**。文件没变的表 SKIP、version 不动；
某张表导入失败也不影响其他表继续递增。
**不同表的 version 数值互不可比**——例如 product=3、label=1 只说明卖点维表改过 3 次、
标签转换维表改过 1 次，不代表谁新谁旧、也不要求两边对齐。

| 操作 | `product_selling_point_match` | `label_name_mapping` |
|---|---|---|
| 首次导入两个文件 | 1 | 1 |
| 改「卖点维表.xlsx」 | **2** | 1（SKIP） |
| 再改「卖点维表.xlsx」 | **3** | 1（SKIP） |
| 改「标签转换维表.xlsx」 | 3（SKIP） | **2** |

`dim_meta` 记录每个表的
`version / source_file / sha256 / file_size / file_mtime / row_count / updated_at`。

## 扩展与运维

### 常见配置变更

| 需求 | 做法 |
|---|---|
| 加字段 / 改字段映射 | 改 `registry.json` 的 `columns[]`，然后**删库重建** |
| 改主键 | 改 `primary_key`，同时调整 `lookup.key` / `cut_in_script.key`，删库重建 |
| 标签列增减 | 改 `dynamic_columns.count`（上限 200），删库重建 |
| 加一张维表 | 在 `tables[]` 追加一项；需要在线读取则声明 `lookup` 或 `cut_in_script` |
| 换库路径 / 轮询间隔 | 改顶层 `database` / `poll_interval_seconds`，或用 `--config` / `--interval` 覆盖 |

> ⚠️ **表结构变更必须删库重建**：`CREATE TABLE IF NOT EXISTS` 不会改已有表结构，
> `ensure_table()` 检测到列集合不一致会抛错并停止导入。执行：
> ```bash
> rm excel2consts/sqlite_db/dimensions.db
> python -m excel2consts.dim_watcher --once
> ```
> 未删库时写入侧每轮报「结构与 registry.json 不一致」，读取侧查询新列失败并
> 降级为空映射（不会 500，但相应维表数据会丢）。不做自动表结构迁移。

### 试跑

```bash
cp io_samples/卖点维表.xlsx io_samples/标签转换维表.xlsx excel2consts/
python -m excel2consts.dim_watcher --once
```

预期：卖点表 1361 行（空主键跳过 187、重复主键覆盖 98）、标签表 60 行，
两表 `version=1`。

### 依赖与约定

- 写入侧只需 `openpyxl`（读 xlsx）；其余为标准库
  （`sqlite3` / `hashlib` / `fcntl` / `logging` / `argparse`）。
  不引入 pandas / yapf，不读 `.env` 或项目总 `config.yaml`。
- `fcntl` 文件锁为 Linux 单机单实例设计，与项目部署环境一致。
- `excel2consts/sqlite_db/`、`state/`、`log/` 是运行时产物，建议加入 `.gitignore`。

### 自检要点（写入侧）

- `dim_watcher --once` 后 `dim_meta` 两行各自 `version=1`、`row_count` 与日志一致。
- 再跑一次：两表都 SKIP、version 不变；改其中一个文件：只有对应表的 version +1。

## 未实现（写入侧）

- 数据表只保留当前版本；版本回滚、历史版本查询、灰度切换未做。
- 不做自动表结构迁移，列集合变化需人工删库重建。
