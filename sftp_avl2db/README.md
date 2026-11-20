# sftp_avl2db · SFTP AVL → SQLite 业务库

每个 AVL 对应一个 db 的写入侧代码包，并内聚了在线读取侧 `data_query.py`，可独立复用。**配置与运行时产出都锚在包内、按 `__file__` 定位，与启动时的 CWD 无关**：`.env` 是唯一配置来源，`runtime/` 是唯一产出目录。

**没有「多库单表 / 单库多表 / 单库单表」模式**：一个 AVL = 一个 `{AVL}.db`，`project`（项目名）只负责把同一业务的本地 AVL 与 db 收进同一对文件夹；表结构按名字放在单独的 `table_schemas.py` 里，多个 AVL 可复用，也可以各不相同。库内表名也不用声明——固定表就叫 `{AVL}`，分表叫 `{AVL}_{分表号}`。

`import sftp_avl2db` 零 I/O、零第三方依赖；组件按「配置 / 解析 / 写入 / I-O / 编排」五层拆分，每层可单独 import、替换、测试。把包 copy 到别的项目时**排除 `.env` 与 `runtime/`**，从 `.env.example` 起步。

## 调用链地图

```
python -m sftp_avl2db --task F09554
  └─ cli.main()                 解析参数、校验日期、装配、返回退出码
     └─ profiles.build_task()  按 profile 拼出 AssembledTask（全部依赖可注入）
        └─ runner.serve_forever()            拿锁 → 单次/常驻 → 清理 → 释放锁
           ├─ runner.run_once_for_date()     建表、连源、遍历该 AVL 的前缀
           │  └─ runner.import_one()        清单 → stat → fetch → parse → write → 登记
           │     ├─ config.remote_name_for()   拼远程文件名（读写唯一来源）
           │     ├─ parsers.*.parse()          生成器产出逻辑记录
           │     ├─ sink.SqliteSink.write()    按 layout 分桶、满批 commit
           │     └─ state.FetchedManifest.add() 成功后才登记
           └─ runner.cleanup_expired()      本地文件 + 库表过期数据
```

读取侧包内 `data_query.py` 只读查询，不参与写入；它与写入侧共用 `avl_specs.py` 的 AVL 声明、`table_schemas.py` 的列顺序与 `shard_index()`。

## 五层与文件

| 层 | 模块 | 顶层依赖 |
|---|---|---|
| 配置 | `config.py`、`avl_specs.py`、`table_schemas.py` | 无 |
| 解析 | `parsers/` | 无 |
| 写入 | `sink/` | 仅标准库 `sqlite3` |
| I/O | `remote.py`、`state.py`、`task_logging.py` | 无（`paramiko` / `loguru` / 锁库延迟导入） |
| 编排 | `runner.py`、`profiles.py`、`cli.py` | 无 |
| 读取 | `data_query.py` | 仅标准库 `sqlite3` + 包内 `config` / `avl_specs` / `table_schemas`；**显式 import 时才读 `.env`** |

公共 API **只认包根** `sftp_avl2db`：`__all__` 列出契约与工厂，I/O 类通过模块级 `__getattr__` 延迟导出。
例外：`data_query` 加载时会读包内 `.env`，因此**不进 `__init__` 顶层导入、也不做隐式延迟导出**；在线侧用 `from sftp_avl2db import data_query` 显式加载，保证 `import sftp_avl2db` 仍零 I/O。`protocols.py` 是唯一契约来源，五个 Protocol 均标了编排器实际用到的方法与属性（含 `RemoteSource.connect()` 与 `RecordSink.stats`），照协议写替身即可替换任一层。

## AVL 与运行

| 项目 project | AVL / `--task` | `.env` 前缀 | schema | 库文件 | 表 |
|---|---|---|---|---|---|
| `opportunity_recall` | `F09554` | `OPPORTUNITY_RECALL_` | `opportunity_recall` | `sqlite_db/opportunity_recall/F09554.db` | `F09554` |
| `opportunity_recall` | `F09555` | `OPPORTUNITY_RECALL_` | `opportunity_recall` | `sqlite_db/opportunity_recall/F09555.db` | `F09555` |
| `opportunity_recall` | `F09556` | `OPPORTUNITY_RECALL_` | `opportunity_recall` | `sqlite_db/opportunity_recall/F09556.db` | `F09556` |
| `loss_control` | `F09591` | `LOSS_CONTROL_` | `loss_control` | `sqlite_db/loss_control/F09591.db` | `F09591_0 … 127` |

```bash
python -m sftp_avl2db --task F09554
python -m sftp_avl2db --task F09591 --date 20261110
python -m sftp_avl2db --task F09591 --date 20261110 --force-reload
```

`--date YYYYMMDD` 为单次模式（拉完指定**文件名日期**后退出），不传则常驻轮询，每轮兜底检查「昨天 + 今天」。`--force-reload` 连清单命中也强制重下重入库。生产**每个 AVL 各起一个常驻进程**，靠 `runtime/state/{AVL}.lock` 保证单机单实例。

注入型选项（离线复跑 / 单测，不改生产默认）：`--local-dir DIR` 把本地目录当远程、不连 SFTP；`--runtime-dir DIR` 把产出隔离到临时目录、不碰包内 `runtime/`；`--max-rounds N` 限制常驻轮数。

远程文件为 `{AVL}{YYYYMMDD}000000.AVL`，制表符分隔、UTF-8；本地落在 `runtime/files/{project}/`，日志 `runtime/log/{AVL}_INFO|ERROR_日期.log` 按天轮转、保留 7 天。

## 目录：项目名是唯一的分组口径

```
runtime/
  files/{project}/{AVL}{日期}000000.AVL      ← 输入
  sqlite_db/{project}/{AVL}.db              ← 输出
  state/{AVL}.fetched  {AVL}.lock
  log/{AVL}_INFO|ERROR_日期.log
```

同一项目的多个 AVL 共用 `files/{project}/` 与 `sqlite_db/{project}/`，库文件名就是 AVL 前缀，不需要额外的映射表；不同项目天然分目录。未来新增一种业务、里面包含很多不同表结构的 AVL，也只是往同一个 `{project}/` 里多放几个 `{AVL}.db`。

## 配置

**唯一来源 `sftp_avl2db/.env`（14 个键），唯一入口 `load_profiles()`**；不读系统环境变量，没有路径覆盖，也没有第二套解析入口。`.env` 语法只认整行 `#` 注释与 `KEY=VALUE`，不做引号、行内注释或 `${VAR}` 插值。缺必需键、非正整数、文件不存在一律 fail-fast（声明漂移会让读写两侧算出不同库表）。改完 `.env` **重启进程**生效（配置对象冻结）。

| 文件 | 作用 |
|---|---|
| `.env` | 与 `config.py` 同目录，日常改这个；`repr` 自动屏蔽 `SFTP_PASSWORD` |
| `.env.example` | 脱敏模板，新环境 `cp .env.example .env` 后填写 |
| `config.py::_AVLS` | `(AVL 名, 项目名)` 声明表；项目名同时是 `.env` 键前缀与运行时目录名 |
| `avl_specs.py` | 每个 AVL 的静态声明：项目、schema 名、解析器、是否分表、查询条件与排序、`PRAGMA synchronous`；`table_name()` 表名口径、`shard_index()` 分表口径；`validate_specs()` 启动校验。**不声明表名、不含产品 ID** |
| `table_schemas.py` | 按名字注册的表结构，读写两侧共用列顺序；**单独一个 py 文件，供多个 AVL 复用** |

`.env` 分组：SFTP 公共 5 项（`SFTP_HOST/PORT/USERNAME/PASSWORD/REMOTE_PATH`），每个项目一组 `<项目名大写>_BATCH_SIZE / DATA_EXPIRE_DAYS / LOCAL_FILE_EXPIRE_DAYS / POLL_INTERVAL_SECONDS`，分表项目另有 `<项目名大写>_NUM_TABLES`。`AVL_SPECS` 与 `_AVLS` 的 key 必须一致，schema 名必须在 `TABLE_SCHEMAS` 里，`validate_specs()` 拦住手填漂移。

`load_profiles()` 不传参 → 产出落在包内 `runtime/`，读写同源靠这一条保证；`load_profiles(runtime_dir=...)` 或 CLI `--runtime-dir` 是唯一隔离入口，只改产出位置、不改配置来源。部署建议 `ln -s /data/crm_runtime sftp_avl2db/runtime` 把数据挂到独立盘 —— **不要 `rm -rf sftp_avl2db/` 整目录覆盖**，包目录现在是代码 + 数据盘。`.env` 含明文密码，仓库只提交 `.env.example`。

## 新增业务 / 新增 AVL 的步骤

1. `table_schemas.py`：每种表结构加一份 `TABLE_SCHEMAS["新名字"]`；结构相同的 AVL 直接复用已有名字。
2. `avl_specs.py`：加一条 `AvlSpec`，写明 `project` / `schema` / `parser` / `sharded` / `where`（恒为 `product_no`）/ `order_by` / `synchronous`；**不写表名、不写产品 ID**——表名自动就是 AVL 名。
3. `config.py::_AVLS`：加一行 `(AVL 名, 项目名)`；项目名决定输入输出目录与 `.env` 键前缀。
4. `.env`：只有**新项目**才需要加一组 `<项目名大写>_*` 参数；已有项目下的新 AVL 直接复用该组参数。
5. 启动：`python -m sftp_avl2db --task {AVL 名}`；在线侧若需按 `product_id` 路由，在 `main.py` 顶部的路由表加一行 `product_id: AVL`，查库统一调 `data_query.query(avl, telnum)`——读取侧不需要任何产品 ID。

## AVL 与库表契约

**业务数据日期 `op_time` 取自 AVL 第 1 列，代码不做日期改写**；文件名 `YYYYMMDD` 是文件生成日（回捞商机 T-2、控损 T-1）。补跑时 `--date` 填文件名日期，不是 `op_time`；过期清理按 `op_time` 列。

**表名不用声明**：固定表就叫 `{AVL}`，分表就叫 `{AVL}_{分表号}`，由 `avl_specs.table_name()` 统一派生，写入侧 `sink/router.py` 与读取侧 `data_query.py` 共用这一条规则。`AvlSpec` 和 `TaskProfileConfig` 里都没有表名或表前缀字段。

- **回捞商机**（`F09554 / F09555 / F09556`）：一行一条、无续行，逻辑 12 列；`product_no`（`avl_specs.PRODUCT_NO_INDEX`）为 `TEXT NOT NULL` 主键与查询键，为空跳过；列数不足补空串、超出截断；`city_id` 非数字或空 → 0。三个 AVL 各写一个 db、各一张与 AVL 同名的表，共用 `opportunity_recall` 表结构。
- **控损**（`F09591`）：逻辑 5 列，`strategy_name` 含裸换行需续行拼接；`product_no` 为主键、分表键、查询键。单个 `F09591.db` 内 128 张表 `F09591_0 … 127`，路由为 `zlib.crc32(product_no.encode("utf-8")) % NUM_TABLES`（由 `avl_specs.shard_index()` 统一实现，读写两侧共用）。写入用 `INSERT OR REPLACE`，批量补跑按文件名日期升序。

改列名/列顺序只改 `table_schemas.py`，改 AVL 归属 / 解析器 / 查询条件只改 `avl_specs.py` 与 `config.py::_AVLS`，改分表数只改 `.env`；读写两侧同时生效。**每个 AVL 只有一个 db 文件**，不存在跨库路由。

## 表名口径变更（部署注意）

这次把表名从 `data_{AVL}` / `data_{AVL}_{分表号}` 改成了 `{AVL}` / `{AVL}_{分表号}`。**库文件路径、表结构、数据都没变，但读写两侧现在查的是新表名**；如果 `runtime/` 里已有旧库，不处理会查不到数据（查询异常被吞掉，返回 `{}`）。新环境或空库可忽略本节。

两种处理方式，选一即可：

- **重建（最简单）**：删掉 `runtime/sqlite_db/` 下旧库与 `runtime/state/*.fetched`，重启写入进程全量重拉。项目本就不要求历史数据迁移。
- **原地改名**：停掉写入进程，对每个库执行 `ALTER TABLE data_X RENAME TO X`：

```python
import sqlite3
from pathlib import Path

for db in Path("sftp_avl2db/runtime/sqlite_db").rglob("*.db"):
    con = sqlite3.connect(str(db))
    olds = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")
        if r[0].startswith("data_")]
    for old in olds:
        con.execute(f'ALTER TABLE "{old}" RENAME TO "{old[5:]}"')
    con.commit()
    con.close()
```

## 刻意保真：不要顺手优化

重构时只改结构定义，**以下行为一律不动**：

1. `FlatColumnParser` 编码探测仍是 `("utf-8", "gbk", "latin-1")` 且带 `errors="ignore"`——实际恒走 utf-8 分支，gbk / latin-1 不会命中；`line.strip()` 连行首行尾制表符一起剥，尾随空列可能被吃掉。
2. 控损续行：以行首 `YYYY-MM-DD` 判定新记录；`strip_eol()` 只去 `\n`、再只剥**一个** ASCII 反斜杠（不能用 `rstrip('\\')`，否则 GBK 尾字节 `0x5C` 的汉字会被削成乱码）；起始行 `>=5` 列取前 5 列丢弃第 6 列起，`==4` 列补空 `strategy_name`，`<4` 列整条跳过；空物理行跳过；文件开头孤立续行丢弃；最后一条单独入库。
3. `sink/schema.py` 三条 SQL 保持拼法，不做参数化改造、不换占位符风格、不改 `PRIMARY KEY (product_no)` 表级声明形式。
4. `SqliteSink` 的差异刻意保留：商机 `PRAGMA synchronous=OFF`、控损 `NORMAL`（由 `avl_specs.AvlSpec.synchronous` 声明，不再由模式推导）；批次容器与提交时机实现不变。连接 PRAGMA 顺序固定为 WAL → synchronous → cache_size → temp_store。
5. 分表口径 `zlib.crc32(product_no) % NUM_TABLES`（`p_no` 为空时取 `"NULL"`，解析器已跳过空主键，属不可达分支）；`INSERT OR REPLACE` + 主键幂等。

## 拉取不变量

- `runtime/state/{AVL}.fetched` 记录已成功入库的远程文件名：每轮先跳过清单命中者，**入库成功后才登记**；失败不登记、本地文件保留、下一轮自动重试。
- `runtime/state/{AVL}.lock` 非阻塞单实例锁，拿不到立即失败；平台无可用锁库时明确报错，不伪装成「已有实例在运行」。
- 所有任务以 `product_no` 为主键 + `INSERT OR REPLACE` 保证幂等，同一手机号只保留一条。
- `exists()` 三态：`False` → INFO「远程文件尚未生成」；`FileNotFoundError` → 同 `False`；其他异常 → ERROR「stat 远程文件出错」。后两者都跳过、不登记清单。

## 在线读取与话术重组

包内 `data_query.py` **只认 AVL 前缀**：唯一入口 `query(avl, telnum)`，`async`、内部同步 `sqlite3`，`SELECT * … LIMIT 1` 按 `op_time DESC` 取最新一条，直接定位 `sqlite_db/{project}/{avl}.db`。

**`product_id` 不进数据访问层**：`main.py` 顶部的 `OPPORTUNITY_RECALL_AVLS` / `LOSS_CONTROL_AVLS` 维护 `product_id → AVL 前缀`，业务路由命中后把 AVL 前缀传进来即可；`avl_specs.py` 里也没有任何产品 ID。表名由 AVL 名派生、列顺序来自声明，用户输入只走参数绑定。库/表缺失、AVL 未注册或查询异常一律返回 `{}`，不会让 API 500。`validate_specs()` 还会拦住把 `product_id` 之类业务字段写进 `where` 的漂移。

**库里保存原始文本**，话术重组在业务侧执行：`rewrite_by_spec_f09554` 拼【通话总结】【用户需求】【已推荐政策】【拒绝原因】四段并追加辅助画像；`rewrite_by_spec_f09591` 把 `<$B>` / `<$I>` / `<$U>` / `<#RRGGBB>` 等自定义标签转成 HTML 并闭合 `</>`。

## 测试与复跑

```bash
python -m sftp_avl2db --task F09591 --date 20261110 \
  --local-dir io_samples --runtime-dir /tmp/replay
```

`build_task()` 的 `logger=`（可用 `InMemoryLogger`）、`local_dir=`、`day=`、`now=`、`sleeper=`、`batch_size=`、`synchronous=`、`table_schema=` 都是显式注入点；`runner.serve_forever()` 另有 `max_rounds` / `date_strings` 测试钩子。离线复跑产出隔离到临时目录，不碰包内 `runtime/`。
