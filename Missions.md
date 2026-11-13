# 定时数据更新任务说明（SFTP AVL 拉取 → SQLite 入库）

> 进程归属：这 3 个任务已从 `main.py` 的 startup 中拆出，各自独立进程运行，代码在 `tasks/`：
> 公共逻辑 `tasks/base_task.py`，各任务 `tasks/task_f09554_opp.py` / `task_f09591_loss.py` / `task_f09609_diag.py`。
> `main.py` 在线业务（FastAPI）只读 SQLite，不再负责拉取。

## 通用规则（3 个任务一致）

1. **远程文件**：SFTP `134.80.199.252:22`，用户 `wangsi`，远程目录 `/ftpdata2/data_reverse/wangsi/aifile`。
   每天生成一个 `{业务前缀}{YYYYMMDD}000000.AVL`，制表符（`\t`）分隔，UTF-8。
2. **不重复拉取**：每个任务维护清单 `tasks/state/{任务名}.fetched`，记录已成功入库的远程文件名；
   每轮轮询先跳过清单里的文件，**入库成功后才登记**。本地已存在同名 `.AVL` 也直接跳过。
3. **断点/失败重试**：解析入库失败的文件不登记清单，下一轮会自动重试；本地文件保留。
4. **跨天兜底**：每轮同时检查「昨天 + 今天」两天的文件名，防止跨天瞬间漏拉。
   > ⚠️ **文件名日期 ≠ 数据日期（op_time）**：`{YYYYMMDD}` 是**文件生成日**，库里的 `op_time` 是业务数据日，两者差 N 天，N 因任务而异（见各任务「文件日期与数据日期」一节）。
   > 补跑时必须以**文件名日期**填 `--date`，例如要补 op_time=2026-11-08 的数据：任务1 用 `--date 20261110`，任务2 用 `--date 20261109`。
5. **单实例锁**：`tasks/state/{任务名}.lock`（portalocker/fcntl 非阻塞锁），同一任务同时只允许一个进程在跑。
6. **轮询间隔**（`POLL_INTERVAL_SECONDS`，可按数据量调）：任务1 = 10 分钟；任务2 = 30 分钟；任务3 = 60 分钟。
7. **本地文件**：下载到 `tasks/files/{任务名}/`，按 `LOCAL_FILE_EXPIRE_DAYS` 天清理。
8. **日志**：`tasks/log/{任务名}_INFO|ERROR_日期.log`，每天轮转，保留 7 天。
9. **运行方式**：
   ```bash
   python tasks/task_f09554_opp.py                 # 常驻轮询（`--date` 传文件名日期，见规则 4）
   python tasks/task_f09591_loss.py --date 20261110 # 补跑文件名日期为 20261110 的文件（=op_time 2026-11-09）后退出
   python tasks/task_f09609_diag.py --date 20261110 --force-reload  # 忽略清单强制重导
   ```
   生产建议用 systemd/nohup 各起一个常驻进程；单机单实例，暂不考虑多机与高可用。

---

# 任务1 回捞商机（号卡 / 套餐 / 宽带）AVL 入库（F09554 / F09555 / F09556）

进程：`tasks/task_f09554_opp.py`；任务名 `f09554_opp`。

## 文件日期与数据日期（关键）

**op_time = 文件名中 YYYYMMDD 的前两天（T-2）**。第 1 列 `op_time` 直接取自文件内容，代码不做日期改写。

| 文件名 | 文件日期 | 入库 op_time |
|---|---|---|
| `F0955420261110000000.AVL` | 2026-11-10 | **2026-11-08** |
| `F0955520261110000000.AVL` | 2026-11-10 | **2026-11-08** |
| `F0955620261110000000.AVL` | 2026-11-10 | **2026-11-08** |

即文件生成当天，库里最新数据只到 2 天前；按今天（文件日期）轮询时，不用再做日期偏移。

## 字段（12 列，一行一条，无续行）

| # | 字段 | 类型 | 说明 |
|---|---|---|---|
| 1 | op_time | TEXT | 数据日期 = **文件名日期 - 2 天**（见上节）；过期清理按此列 |
| 2 | call_time | TEXT | 通话日期 |
| 3 | product_no | TEXT | 手机号（在线查询的键） |
| 4 | type_dl | VARCHAR(50) | 大类（如「号卡业务」） |
| 5 | type_xl | VARCHAR(50) | 小类（如「新开卡需求」） |
| 6 | content_abstract | TEXT | 通话摘要 |
| 7 | cust_demand | TEXT | 客户需求 |
| 8 | rec_strategy | TEXT | 推荐策略 |
| 9 | reject_reason | TEXT | 拒绝原因 |
| 10 | reason_note | TEXT | 原因备注 |
| 11 | refer_strategy | TEXT | 参考策略 |
| 12 | city_id | INTEGER | 地市编码；非数字/空 → 0 |

- 列数不足补空串，超出截断；入库方式 `INSERT`（追加），无主键。
- 库表：每个前缀一个库 `tasks/sqlite_db/f09554_opp/{F09554,F09555,F09556}.db`，
  库内表名 `data_{前缀}`，索引 `(product_no, op_time)`。
  与在线查询 `PRODUCT_TABLE_MAP`（202608041615335173/172/174）对应。
- 保留：库数据 30 天，本地 AVL 7 天。
- 量级：每个 AVL 每天不到 1 万条，单库单表即可。

# 任务2 控损 AVL 入库（F09591）

进程：`tasks/task_f09591_loss.py`；任务名 `f09591_loss`。

## 文件日期与数据日期（关键）

**op_time = 文件名中 YYYYMMDD 的前一天（T-1）**。第 1 列 `op_time` 直接取自文件内容，代码不做日期改写。

| 文件名 | 文件日期 | 入库 op_time |
|---|---|---|
| `F0959120261110000000.AVL` | 2026-11-10 | **2026-11-09** |


## 字段（逻辑 5 列）

| # | 字段 | 类型 | 说明 |
|---|---|---|---|
| 1 | op_time | TEXT | 数据日期 = **文件名日期 - 1 天**（见上节）；过期清理按此列 |
| 2 | user_id | TEXT | 用户标识 |
| 3 | product_no | TEXT | 手机号（分表键、查询键） |
| 4 | city_id | TEXT | 地市编码 |
| 5 | strategy_name | TEXT | 控损策略正文（**含裸换行，需续行拼接**） |

## 续行拼接规则（关键）

1. 逐物理行读取；以 `YYYY-MM-DD` 开头的物理行 = 一条新逻辑记录的起始行。
2. 不以日期开头的物理行 = **续行**，属于上一条记录的第 5 列 `strategy_name`。
3. 续行拼回第 5 列时用 `\n` 还原换行（`buffer[4] += "\n" + 续行`）。
4. 每个物理行行尾可能带一个续行标记反斜杠 `\`，读取时去掉。
   注意：只去掉行尾**一个 ASCII 反斜杠**；不能用 `rstrip('\\')`，
   否则 GBK 下尾字节为 `0x5C` 的汉字会被削掉一个字节变成乱码。
5. 起始行按 `\t` 切分：`>=5` 列 → 前 4 列为元数据，第 5 列及之后拼成 `strategy_name`；
   `==4` 列 → `strategy_name` 为空；`<4` 列 → 视为损坏行，跳过。
6. 文件开头就出现续行（没有上文）→ 丢弃；文件读完时最后一条记录要单独入库。

## 库表与分表

- 单库 `tasks/sqlite_db/f09591_loss/F09591.db`，128 张表 `data_F09591_0 … data_F09591_127`。
- 路由：`表号 = zlib.crc32(product_no.encode('utf-8')) % 128`（product_no 为空时用 `"NULL"`），
  与在线查询 `data_query.get_latest_record` 完全一致。
- 索引 `(product_no, op_time)`；入库方式 `INSERT` 追加。
- 保留：库数据 50 天，本地 AVL 50 天。
- 量级：每个 AVL 约 7000 万条 → 每表日均约 55 万条；PRAGMA 用 `synchronous = NORMAL` 平衡安全与速度。

# 任务3 诊断 AVL 入库（F09609）

进程：`tasks/task_f09609_diag.py`；任务名 `f09609_diag`。

## 文件日期与数据日期（待业务确认）

op_time 同样直接取自文件第 1 列，代码不做日期改写。当前样例 `F0960920261110000000.AVL` 内的 op_time 是 **2026-11-08**，比文件名日期早 2 天（T-2）。

## 字段（宽表，逻辑记录展开后 5 列）

| # | 字段 | 类型 | 说明 |
|---|---|---|---|
| 1 | op_time | TEXT | 数据日期 = 文件第 1 列原值（偏移量见上节）；过期清理按此列 |
| 2 | user_id | TEXT | 用户标识 |
| 3 | product_no | TEXT | 手机号（分表键、查询键之一） |
| 4 | product_id | TEXT NOT NULL | 产品 ID（与 product_no 联合主键） |
| 5 | strategy_script | TEXT | 该产品对应的策略/话术（**含裸换行**） |

## 续行与展开规则（关键）

1. 同样以「行首是否 `YYYY-MM-DD`」区分新记录起始行与续行，续行合并进当前逻辑记录。
2. 一条逻辑记录的所有物理行先收集齐，再用 `\n` **join 成整段文本后统一按 `\t` 切列**
   （这样即使 tab 被换行拆开也能正确切分）。
3. 切列后：第 1~3 列 = `op_time / user_id / product_no`；
   **第 4 列起每两列一组 `(product_id, strategy_script)`**，每组生成一条入库记录。
4. `product_id` 为空（纯空白）的组跳过；总列数 `< 3` 的逻辑记录整条跳过并告警。
5. 行尾反斜杠处理同任务2（只剥一个 ASCII 反斜杠）。

## 库表与分表

- 单库 `tasks/sqlite_db/f09609_diag/F09609.db`，128 张表 `data_F09609_0 … data_F09609_127`。
- 路由同任务2：`zlib.crc32(product_no) % 128`，与 `data_query.get_record` 一致。
- 主键 `PRIMARY KEY (product_no, product_id)`，入库 `INSERT OR REPLACE`：
  **重复拉取/补导天然幂等**，不会产生重复行。
- 保留：库数据 50 天，本地 AVL 50 天。
- 量级：拆分后每天约 3 亿条 → 每表日均约 234 万条；`synchronous = NORMAL`。
  若单机入库速度不够，可增大 `BATCH_SIZE` 或调低轮询频率，暂不做多进程分片。

---

## 补充/待确认事项

- `F09554/55/56` 入库无唯一键，重复执行 `--force-reload` 会产生重复行（原脚本行为如此，未改）；
  如需幂等，建议加 `PRIMARY KEY(op_time, call_time, product_no, type_dl, type_xl)` 并改 `INSERT OR REPLACE`，待业务确认后调整。
- 三个任务的 SFTP 账号密码、过期天数等仍从原 `sftp_service.py` 的 `CONFIG` 读取，避免两处配置漂移；
  后续可统一迁到 `config.yaml`。
- 样本文件 `io_samples/*.AVL` 中：F09591/F09609 每条逻辑记录有 2 条物理记录、多组产品时列数为 9（3 + 3 组）；
  生产上 F09609 约 103 列（约 50 组），解析逻辑与列数无关。
- **op_time 偏移量**：任务1 = T-2、任务2 = T-1（已确认）；任务3 目前按样例推断为 T-2，待生产数据核实。
  清理过期数据（`DATA_EXPIRE_DAYS`）比较的是 op_time，因此保留天数实际覆盖的文件日期要多出对应的偏移天数。
