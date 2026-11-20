# sftp_avl2db · SFTP AVL → SQLite 业务库

两个定时任务（回捞商机 / 控损）的写入侧代码包，并内聚了在线读取侧 `data_query.py`，可独立复用。**配置与运行时产出都锚在包内、按 `__file__` 定位，与启动时的 CWD 无关**：`.env` 是唯一配置来源，`runtime/` 是唯一产出目录。

`import sftp_avl2db` 零 I/O、零第三方依赖；组件按「配置 / 解析 / 写入 / I-O / 编排」五层拆分，每层可单独 import、替换、测试。把包 copy 到别的项目时**排除 `.env` 与 `runtime/`**，从 `.env.example` 起步。

## 调用链地图

```
python -m sftp_avl2db
  └─ cli.main()                 解析参数、校验日期、装配、返回退出码
     └─ profiles.build_task()  按 profile 拼出 AssembledTask（全部依赖可注入）
        └─ runner.serve_forever()            拿锁 → 单次/常驻 → 清理 → 释放锁
           ├─ runner.run_once_for_date()     建表、连源、遍历 file_prefixes
           │  └─ runner.import_one()        清单 → stat → fetch → parse → write → 登记
           │     ├─ config.remote_name_for()   拼远程文件名（读写唯一来源）
           │     ├─ parsers.*.parse()          生成器产出逻辑记录
           │     ├─ sink.SqliteSink.write()    按 layout 分桶、满批 commit
           │     └─ state.FetchedManifest.add() 成功后才登记
           └─ runner.cleanup_expired()      本地文件 + 库表过期数据
```

读取侧包内 `data_query.py` 只读查询，不参与写入；它与写入侧共用 `dbspec.py` 的列顺序、`QueryMode` 与 `shard_index()`。

## 五层与文件

| 层 | 模块 | 顶层依赖 |
|---|---|---|
| 配置 | `config.py`、`dbspec.py` | 无 |
| 解析 | `parsers/` | 无 |
| 写入 | `sink/` | 仅标准库 `sqlite3` |
| I/O | `remote.py`、`state.py`、`task_logging.py` | 无（`paramiko` / `loguru` / 锁库延迟导入） |
| 编排 | `runner.py`、`profiles.py`、`cli.py` | 无 |
| 读取 | `data_query.py` | 仅标准库 `sqlite3` + 包内 `config` / `dbspec`；**显式 import 时才读 `.env`** |

公共 API **只认包根** `sftp_avl2db`：`__all__` 列出契约与工厂，I/O 类通过模块级 `__getattr__` 延迟导出。
例外：`data_query` 加载时会读包内 `.env`，因此**不进 `__init__` 顶层导入、也不做隐式延迟导出**；在线侧用 `from sftp_avl2db import data_query` 显式加载，保证 `import sftp_avl2db` 仍零 I/O。`protocols.py` 是唯一契约来源，五个 Protocol 均标了编排器实际用到的方法与属性（含 `RemoteSource.connect()` 与 `RecordSink.stats`），照协议写替身即可替换任一层。

## 任务与运行

| 业务 | `--task` | `.env` 前缀 | AVL 文件前缀 |
|---|---|---|---|
| 回捞商机 | `opportunity_recall` | `OPPORTUNITY_RECALL_` | F09554 / F09555 / F09556 |
| 控损 | `loss_control` | `LOSS_CONTROL_` | F09591 |

```bash
python -m sftp_avl2db --task opportunity_recall
python -m sftp_avl2db --task loss_control --date 20261110
python -m sftp_avl2db --task loss_control --date 20261110 --force-reload
```

`--date YYYYMMDD` 为单次模式（拉完指定**文件名日期**后退出），不传则常驻轮询，每轮兜底检查「昨天 + 今天」。`--force-reload` 连清单命中也强制重下重入库。

注入型选项（离线复跑 / 单测，不改生产默认）：`--local-dir DIR` 把本地目录当远程、不连 SFTP；`--runtime-dir DIR` 把产出隔离到临时目录、不碰包内 `runtime/`；`--max-rounds N` 限制常驻轮数。生产每个任务各起一个常驻进程，靠 `runtime/state/{任务名}.lock` 保证单机单实例。

远程文件为 `{业务前缀}{YYYYMMDD}000000.AVL`，制表符分隔、UTF-8；本地落在 `runtime/files/{任务名}/`，日志 `runtime/log/{任务名}_INFO|ERROR_日期.log` 按天轮转、保留 7 天。

## 配置

**唯一来源 `sftp_avl2db/.env`（17 个键），唯一入口 `load_profiles()`**；不读系统环境变量，没有路径覆盖，也没有第二套解析入口。`.env` 语法只认整行 `#` 注释与 `KEY=VALUE`，不做引号、行内注释或 `${VAR}` 插值。缺必需键、非正整数、文件不存在一律 fail-fast（库表配置漂移会让读写两侧算出不同库表）。改完 `.env` **重启进程**生效（配置对象冻结）。

| 文件 | 作用 |
|---|---|
| `.env` | 与 `config.py` 同目录，日常改这个；`repr` 自动屏蔽 `SFTP_PASSWORD` |
| `.env.example` | 脱敏模板，新环境 `cp .env.example .env` 后填写 |
| `config.py::_TASKS` | 任务声明（名 / 键前缀 / 解析器 / 是否分表）；新增任务加一行 + 写进 `.env` |
| `dbspec.py` | 包内静态契约：`DB_SCHEMAS`（建表 DDL 与读取侧 `SELECT *` 组装 dict 共用列顺序）、`QUERY_SPECS`（在线查询声明）、`QueryMode`（库表形式常量）、`shard_index()`（分表函数）、`validate_specs()`（启动校验） |

`PROFILES` / `DB_SCHEMAS` / `QUERY_SPECS` 三者 key 必须一致；`mode`、`table_name` 由 `QUERY_SPECS` 派生到 profile，`validate_specs()` 拦住手填漂移。

`load_profiles()` 不传参 → 产出落在包内 `runtime/`（`sqlite_db` / `state` / `files` / `log`），读写同源靠这一条保证；`load_profiles(runtime_dir=...)` 或 CLI `--runtime-dir` 是唯一隔离入口，只改产出位置、不改配置来源。部署建议 `ln -s /data/crm_runtime sftp_avl2db/runtime` 把数据挂到独立盘 —— **不要 `rm -rf sftp_avl2db/` 整目录覆盖**，包目录现在是代码 + 数据盘。`.env` 含明文密码，仓库只提交 `.env.example`。

`.env` 分组：SFTP 公共 5 项（`SFTP_HOST/PORT/USERNAME/PASSWORD/REMOTE_PATH`），每个任务一组 `<前缀>_FILE_PREFIXES / BATCH_SIZE / DATA_EXPIRE_DAYS / LOCAL_FILE_EXPIRE_DAYS / POLL_INTERVAL_SECONDS`，控损另有 `NUM_TABLES` / `TABLE_NAME_PREFIX`。

## AVL 与库表契约

**业务数据日期 `op_time` 取自 AVL 第 1 列，代码不做日期改写**；文件名 `YYYYMMDD` 是文件生成日（回捞商机 T-2、控损 T-1）。补跑时 `--date` 填文件名日期，不是 `op_time`；过期清理按 `op_time` 列。

- **回捞商机**：一行一条、无续行，逻辑 12 列；`product_no`（`dbspec.PRODUCT_NO_INDEX`）为 `TEXT NOT NULL` 主键与查询键，为空跳过；列数不足补空串、超出截断；`city_id` 非数字或空 → 0。每个前缀一个库 `{前缀}.db`，表名 `data_{前缀}`；`QUERY_SPECS["opportunity_recall"]["db_map"]` 把 product_id 映射到库名。
- **控损**：逻辑 5 列，`strategy_name` 含裸换行需续行拼接；`product_no` 为主键、分表键、查询键。单库 128 张表 `data_F09591_0 … 127`，路由为 `zlib.crc32(product_no.encode("utf-8")) % NUM_TABLES`（由 `dbspec.shard_index()` 统一实现，读写两侧共用）。写入用 `INSERT OR REPLACE`，批量补跑按文件名日期升序。

库表形式由 `dbspec.QueryMode` 三个常量唯一定义（多库多表不存在）；写入侧 `sink/router.py::build_layout()` 与读取侧 `_resolve()` 按同一组值分支：

| mode | 库文件 | 表名 | 专用字段 |
|---|---|---|---|
| `MULTI_DB_SINGLE_TABLE` | 按 product_id 选 `{库名}.db` | 每库一张 `data_{库名}` | `db_map` |
| `SINGLE_DB_MULTI_TABLE` | `{FILE_PREFIXES[0]}.db` | `{TABLE_NAME_PREFIX}_{分表号}` | `.env` 分表配置 |
| `SINGLE_DB_SINGLE_TABLE` | `{FILE_PREFIXES[0]}.db` | `QUERY_SPECS` 的 `table_name` 固定表 | 预留，当前无任务使用 |

改列名/列顺序只改 `DB_SCHEMAS`，改条件或排序只改 `QUERY_SPECS`，改分表数/前缀只改 `.env`；读写两侧同时生效。

## 刻意保真：不要顺手优化

重构时只做重命名、抽函数、加类型与注释，**以下行为一律不动**，否则产出与历史库不一致：

1. `FlatColumnParser` 编码探测仍是 `("utf-8", "gbk", "latin-1")` 且带 `errors="ignore"`——实际恒走 utf-8 分支，gbk / latin-1 不会命中；`line.strip()` 连行首行尾制表符一起剥，尾随空列可能被吃掉。
2. 控损续行：以行首 `YYYY-MM-DD` 判定新记录；`strip_eol()` 只去 `\n`、再只剥**一个** ASCII 反斜杠（不能用 `rstrip('\\')`，否则 GBK 尾字节 `0x5C` 的汉字会被削成乱码）；起始行 `>=5` 列取前 5 列丢弃第 6 列起，`==4` 列补空 `strategy_name`，`<4` 列整条跳过；空物理行跳过；文件开头孤立续行丢弃；最后一条单独入库。
3. `sink/schema.py` 三条 SQL 保持拼法，不做参数化改造、不换占位符风格、不改 `PRIMARY KEY (product_no)` 表级声明形式。
4. `SqliteSink` 的商机 / 控损差异刻意保留：商机 `PRAGMA synchronous=OFF`、单表一个批次容器；控损 `NORMAL`、128 表各自容器、任一表满批只提交该表。连接 PRAGMA 顺序固定为 WAL → synchronous → cache_size → temp_store。
5. 分表口径 `zlib.crc32(product_no) % NUM_TABLES`（`p_no` 为空时取 `"NULL"`，解析器已跳过空主键，属不可达分支）；`INSERT OR REPLACE` + 主键幂等。

## 拉取不变量

- `runtime/state/{任务名}.fetched` 记录已成功入库的远程文件名：每轮先跳过清单命中者，**入库成功后才登记**；失败不登记、本地文件保留、下一轮自动重试。
- `runtime/state/{任务名}.lock` 非阻塞单实例锁，拿不到立即失败；平台无可用锁库时明确报错，不伪装成「已有实例在运行」。
- 所有任务以 `product_no` 为主键 + `INSERT OR REPLACE` 保证幂等，同一手机号只保留一条。
- `exists()` 三态：`False` → INFO「远程文件尚未生成」；`FileNotFoundError` → 同 `False`；其他异常 → ERROR「stat 远程文件出错」。后两者都跳过、不登记清单。

## 在线读取与话术重组

包内 `data_query.py` 按任务名派发：`query(task_name, telnum, product_id=None)` 及薄封装 `get_opportunity_recall_latest()` / `get_loss_control_latest()`，均为 `async`、内部同步 `sqlite3`，`SELECT * … LIMIT 1` 按 `op_time DESC` 取最新一条；表名与列名来自 `dbspec`，用户输入只走参数绑定。库/表缺失、product_id 未映射、任务名未注册或查询异常一律返回 `{}`，不会让 API 500。

**库里保存原始文本**，话术重组在业务侧执行：`rewrite_by_spec_f09554` 拼【通话总结】【用户需求】【已推荐政策】【拒绝原因】四段并追加辅助画像；`rewrite_by_spec_f09591` 把 `<$B>` / `<$I>` / `<$U>` / `<#RRGGBB>` 等自定义标签转成 HTML 并闭合 `</>`。

## 测试与复跑

```bash
python -m sftp_avl2db --task loss_control --date 20261110 \
  --local-dir io_samples --runtime-dir /tmp/replay
```

`build_task()` 的 `logger=`（可用 `InMemoryLogger`）、`local_dir=`、`day=`、`now=`、`sleeper=`、`batch_size=`、`synchronous=`、`table_schema=` 都是显式注入点；`runner.serve_forever()` 另有 `max_rounds` / `date_strings` 测试钩子。离线复跑产出隔离到临时目录，不碰包内 `runtime/`。
