# sftp_avl2db · SFTP AVL → SQLite 业务库

每个 AVL 对应一个 db 的**纯写入侧**代码包，可独立复用：只负责
「SFTP 拉取 AVL → 解析 → 写进 SQLite」，**不提供在线查询入口，也不声明在线侧的
库路径、表字段或查询条件**。在线业务侧读哪些库、按什么键查由其自身维护；
两侧唯一的交集是磁盘上的 db 文件，没有任何代码 / 配置模块互相引用。

**配置与运行时产出都锚在包内、按 `__file__` 定位，与启动时的 CWD 无关**：
`.env` 是本包唯一配置来源，`runtime/` 是唯一产出目录。

一个 AVL = 一个 `{AVL}.db`，`project`（项目名）负责把同一业务的本地 AVL 与 db 收进同一对文件夹；表结构按名字放在 `table_schemas.py` 里，多个 AVL 可复用，也可以各不相同。主键按 schema 名注册在 `TABLE_PRIMARY_KEYS`，支持复合主键，各表按自身业务定位键建主键。库内固定表叫 `{AVL}`，分表叫 `{AVL}_{分表号}`。

`import sftp_avl2db` 零 I/O、零第三方依赖；组件按「配置 / 解析 / 写入 / I-O / 编排」五层拆分，每层可单独 import、替换、测试。把包 copy 到别的项目时**排除 `.env` 与 `runtime/`**，从 `.env.example` 起步。

## 调用链地图

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
           └─ runner.cleanup_expired()      本地文件 + 库表过期数据
```

## 五层与文件

| 层 | 模块 | 顶层依赖 |
|---|---|---|
| 配置 | `config.py`、`avl_specs.py`、`table_schemas.py` | 无 |
| 解析 | `parsers/` | 无 |
| 写入 | `sink/` | 仅标准库 `sqlite3` |
| I/O | `remote.py`、`state.py`、`task_logging.py` | 无（`paramiko` / `loguru` / 锁库延迟导入） |
| 编排 | `runner.py`、`profiles.py`、`cli.py` | 无 |

公共 API **只认包根** `sftp_avl2db`：`__all__` 列出契约与工厂，I/O 类通过模块级 `__getattr__` 延迟导出，保证 `import sftp_avl2db` 零 I/O。`protocols.py` 是唯一契约来源，五个 Protocol 均标了编排器实际用到的方法与属性（含 `RemoteSource.connect()` 与 `RecordSink.stats`），照协议写替身即可替换任一层。

## AVL 与运行

| 项目 project | AVL / `--task` | `.env` 前缀 | schema | 库文件 | 表 |
|---|---|---|---|---|---|
| `opportunity_recall` | `F09554` | `OPPORTUNITY_RECALL_` | `opportunity_recall` | `sqlite_db/opportunity_recall/F09554.db` | `F09554` |
| `opportunity_recall` | `F09555` | `OPPORTUNITY_RECALL_` | `opportunity_recall` | `sqlite_db/opportunity_recall/F09555.db` | `F09555` |
| `opportunity_recall` | `F09556` | `OPPORTUNITY_RECALL_` | `opportunity_recall` | `sqlite_db/opportunity_recall/F09556.db` | `F09556` |
| `loss_control` | `F09591` | `LOSS_CONTROL_` | `loss_control` | `sqlite_db/loss_control/F09591.db` | `F09591_0 … 127` |
| `diagnosis` | `F09627` | `DIAGNOSIS_` | `scene_rank` | `sqlite_db/diagnosis/F09627.db` | `F09627` |
| `diagnosis` | `F09628` | `DIAGNOSIS_` | `product_scene` | `sqlite_db/diagnosis/F09628.db` | `F09628` |
| `diagnosis` | `F09629` | `DIAGNOSIS_` | `product_business` | `sqlite_db/diagnosis/F09629.db` | `F09629` |
| `diagnosis` | `F09630` | `DIAGNOSIS_` | `scene_qrhuashu` | `sqlite_db/diagnosis/F09630.db` | `F09630` |
| `diagnosis` | `F09631` | `DIAGNOSIS_` | `product_yxhuashu` | `sqlite_db/diagnosis/F09631.db` | `F09631` |
| `diagnosis` | `F09632` | `DIAGNOSIS_` | `product_wlhuashu` | `sqlite_db/diagnosis/F09632.db` | `F09632` |
| `diagnosis` | `F09633` | `DIAGNOSIS_` + `F09633_NUM_TABLES` | `conf_user_scene` | `sqlite_db/diagnosis/F09633.db` | `F09633_0 … 127` |
| `diagnosis` | `F09635` | `DIAGNOSIS_` | `tagid_tagname` | `sqlite_db/diagnosis/F09635.db` | `F09635` |

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

**唯一来源 `sftp_avl2db/.env`（14 个键），唯一入口 `load_profiles()`**；不读系统环境变量，没有路径覆盖，也没有第二套解析入口。`.env` 语法只认整行 `#` 注释与 `KEY=VALUE`，不做引号、行内注释或 `${VAR}` 插值。缺必需键、非正整数、文件不存在一律 fail-fast。改完 `.env` **重启进程**生效（配置对象冻结）。

| 文件 | 作用 |
|---|---|
| `.env` | 与 `config.py` 同目录，日常改这个；`repr` 自动屏蔽 `SFTP_PASSWORD` |
| `.env.example` | 脱敏模板，新环境 `cp .env.example .env` 后填写 |
| `config.py::_AVLS` | `(AVL 名, 项目名)` 声明表；项目名同时是 `.env` 键前缀与运行时目录名 |
| `avl_specs.py` | 每个 AVL 的写入侧静态声明：项目、schema 名、解析器、是否分表；`table_name()` 表名规则、`shard_index()` 分表口径、`validate_specs()` 启动校验。**不声明表名、不含产品 ID、不含 PRAGMA，也不含任何在线查询条件 / 排序** |
| `table_schemas.py` | 按名字注册的表结构（`TABLE_SCHEMAS`）与主键（`TABLE_PRIMARY_KEYS`），供写入侧建表 DDL、幂等与解析器列数校验使用；**单独一个 py 文件，供多个 AVL 复用** |

`.env` 分组：SFTP 公共 5 项（`SFTP_HOST/PORT/USERNAME/PASSWORD/REMOTE_PATH`），每个项目一组 `<项目名大写>_BATCH_SIZE / DATA_EXPIRE_DAYS / LOCAL_FILE_EXPIRE_DAYS / POLL_INTERVAL_SECONDS`，分表 AVL 另有 `NUM_TABLES`。分表数按 **`<AVL>_NUM_TABLES` 优先、`<项目名大写>_NUM_TABLES` 兜底** 读取（`config._num_tables()`），因此同一项目下可以只让个别大表分表：`F09591` 读 `LOSS_CONTROL_NUM_TABLES`，`F09633` 读 `F09633_NUM_TABLES`，diagnosis 其余 7 张维表仍为固定表。`AVL_SPECS` 与 `_AVLS` 的 key 必须一致，schema 名必须在 `TABLE_SCHEMAS` 里，主键必须在 `TABLE_PRIMARY_KEYS` 里；分表 AVL 的表结构还必须含 `product_no` 列。这些漂移都由 `validate_specs()` 在启动时拦住。

`load_profiles()` 不传参 → 产出落在包内 `runtime/`；`load_profiles(runtime_dir=...)` 或 CLI `--runtime-dir` 是唯一隔离入口，只改产出位置、不改配置来源。部署建议 `ln -s /data/crm_runtime sftp_avl2db/runtime` 把数据挂到独立盘 —— **不要 `rm -rf sftp_avl2db/` 整目录覆盖**，包目录现在是代码 + 数据盘。`.env` 含明文密码，仓库只提交 `.env.example`。

> 在线业务侧读取这些 db 文件时，库路径、表字段与分表数由 `services/db_config.py`
> 与 `services/.env` **独立声明**（不读本包 `.env`）。改列顺序 / 分表数 / 库路径时
> 两侧声明必须同步，详见[解耦改造说明](../解耦改造说明.md)。

## 新增业务 / 新增 AVL 的步骤

1. `table_schemas.py`：每种表结构加一份 `TABLE_SCHEMAS["新名字"]`，并在 `TABLE_PRIMARY_KEYS` 里按同名注册主键（单列或复合）；结构相同的 AVL 直接复用已有名字。
2. `avl_specs.py`：加一条 `AvlSpec`，写明 `project` / `schema` / `parser` / `sharded`；**不写表名、不写主键、不写 PRAGMA、不写查询条件**——表名自动就是 AVL 名，主键来自第 1 步。设 `sharded=True` 时表结构还须含 `product_no` 列。一行一条的表统一用 `parser="flat"`，列数 / 主键列 / 跳过规则全部由 profile 的注册信息驱动，不需要新增解析器类。
3. `config.py::_AVLS`：加一行 `(AVL 名, 项目名)`；项目名决定输入输出目录与 `.env` 键前缀。
4. `.env`：只有**新项目**才需要加一组 `<项目名大写>_*` 参数；已有项目下的新 AVL 直接复用该组参数。分表 AVL 另加 `<AVL>_NUM_TABLES`（优先）或项目级 `<项目名大写>_NUM_TABLES`，见上文配置说明。
5. 启动：`python -m sftp_avl2db --task {AVL 名}`。若在线业务侧也要读这个新库，在 `services/db_config.py` 补一份对应的在线侧声明（列顺序、db 文件、查询键），两侧不互相引用。

## AVL 与库表契约

**业务数据日期 `op_time` 取自 AVL 第 1 列，代码不做日期改写**；文件名 `YYYYMMDD` 是文件生成日（回捞商机 T-2、控损 T-1）。补跑时 `--date` 填文件名日期，不是 `op_time`；过期清理按 `op_time` 列。

**表名不用声明**：固定表就叫 `{AVL}`，分表就叫 `{AVL}_{分表号}`，由 `avl_specs.table_name()` 统一派生，写入侧 `sink/router.py` 建表与逐行路由都用它。`AvlSpec` 和 `TaskProfileConfig` 里都没有表名或表前缀字段。

- **回捞商机**（`F09554 / F09555 / F09556`）：一行一条、无续行，逻辑 12 列；`product_no`（`avl_specs.PRODUCT_NO_INDEX`）为 `TEXT NOT NULL` 主键，为空跳过；列数不足补空串、超出截断，所有列原样保留文本（末列 `city_id` 声明为 INTEGER，由 SQLite 列亲和性转换；业务上不消费该字段）。三个 AVL 各写一个 db、各一张与 AVL 同名的表，共用 `opportunity_recall` 表结构。
- **控损**（`F09591`）：逻辑 5 列，`strategy_name` 含裸换行需续行拼接；`product_no` 为主键与分表键。单个 `F09591.db` 内 128 张表 `F09591_0 … 127`，路由为 `zlib.crc32(product_no.encode("utf-8")) % NUM_TABLES`（由 `avl_specs.shard_index()` 统一实现）。写入用 `INSERT OR REPLACE`，批量补跑按文件名日期升序。
- **诊断·用户场景对应关系**（`F09633`）：诊断 8 表里唯一带 `product_no` 的表，千万级行，同样按 `product_no` 分 128 张表 `F09633_0 … 127`。分表数用 `F09633_NUM_TABLES` 单独配置，不影响同项目其它 7 张固定表。详见下文「F09633 分表」。

改列名/列顺序只改 `table_schemas.py`，改 AVL 归属 / 解析器只改 `avl_specs.py` 与 `config.py::_AVLS`，改主键只改 `TABLE_PRIMARY_KEYS`，改分表数只改 `.env`；**每个 AVL 只有一个 db 文件**，不存在跨库路由。分表路由用的 `product_no` 列下标由 `build_layout()` 从注册的表结构推导，不硬编码第 3 列。

## 诊断新线：8 张表注册（project = diagnosis）

| AVL | 中文名 | schema | 列（按 AVL 顺序） | 主键 |
|---|---|---|---|---|
| `F09627` | 场景优先级 | `scene_rank` | `op_time, scene, rn` | `scene` |
| `F09628` | 产品-场景 | `product_scene` | `op_time, business_type, scene` | `business_type, scene` |
| `F09629` | 产品策略 | `product_business` | `op_time, business_type, product_id, business_name` | `product_id` |
| `F09630` | 场景-切入话术 | `scene_qrhuashu` | `op_time, scene, qr_huashu` | `scene` |
| `F09631` | 产品-营销话术 | `product_yxhuashu` | `op_time, business_type, yx_huashu` | `business_type, yx_huashu` |
| `F09632` | 产品-挽留话术 | `product_wlhuashu` | `op_time, business_type, reject_reason, wl_huashu` | `business_type, reject_reason, wl_huashu` |
| `F09633` | 用户场景对应关系（**分表**） | `conf_user_scene` | `op_time, user_id, product_no, scene, reason` | `product_no, scene` |
| `F09635` | 标签ID-标签名 | `tagid_tagname` | `op_time, tag_id, tag_name, unknown1, unknown2` | `tag_id` |

**主键口径**：`op_time` 不进主键，维表每日全量按主键 `INSERT OR REPLACE` 覆盖最新批次（与商机 / 控损一致）；主键列自动补 `NOT NULL`。F09631 / F09632 把话术全文纳入主键，是为了避免样例中的重复行、以及「同一产品 + 同一拒绝原因对应两条不同挽留话术」被 REPLACE 丢行。F09633 主键 `(product_no, scene)` 与分表键共用 `product_no`：同一号码一定落在同一分表，主键在单表内仍然唯一，幂等语义不变。

**口径裁决**：
- F09629 的 `business_name` 含【】内外文本：按 `product_id` 取该字段后直接拆分，【】内为资费名称、【】外为资费摘要。

**离线复跑**（8 个 AVL 各 10 行样例，日期按文件名取 `20261120`）：

```bash
for t in F09627 F09628 F09629 F09630 F09631 F09632 F09633 F09635; do
  python -m sftp_avl2db --task $t --date 20261120 \
    --local-dir io_samples --runtime-dir /tmp/replay
done
```

表结构与主键见 `table_schemas.py`，静态声明见 `avl_specs.py`，AVL 归属见 `config.py::_AVLS`，`.env` 参数组见 `.env.example` 的 `DIAGNOSIS_*`。在线业务侧的取数链路见根 [`诊断新线_在线链路实现说明.md`](../诊断新线_在线链路实现说明.md) 与 [`services/README.md`](../services/README.md)。

## 解析与写入行为

1. `FlatColumnParser` 是唯一的 flat 解析器，商机回捞与诊断 8 张维表共用：固定按 UTF-8 读取（带 `errors="ignore"`），只去行尾换行 / BOM、保留首尾制表符与空列，按 `\t` 切分后补齐或截断到 `profile.columns` 声明的列数。**所有列原样保留文本、不做任何数值转换**：列类型交给 SQLite 列亲和性（商机末列 `city_id` 用不上的字段也照此处理）。空主键判定的列下标由 `build_parser()` 按 `profile.primary_key` 换算，与建表 DDL 同源；商机是第 3 列 `product_no`，诊断维表是各自注册的单列 / 复合主键。
2. 控损续行：以行首 `YYYY-MM-DD` 判定新记录；`strip_eol()` 只去 `\n`、再只剥**一个** ASCII 反斜杠（不能用 `rstrip('\\')`，否则 GBK 尾字节 `0x5C` 的汉字会被削成乱码）；起始行 `>=5` 列取前 5 列丢弃第 6 列起，`==4` 列补空 `strategy_name`，`<4` 列整条跳过；空物理行跳过；文件开头孤立续行丢弃；最后一条单独入库。
3. `sink/schema.py` 三条 SQL 保持拼法，不做参数化改造、不换占位符风格、保持表级 `PRIMARY KEY (...)` 声明形式；主键列由 `TABLE_PRIMARY_KEYS` 传入，可单列可复合，且自动补 `NOT NULL`（主键列已在列定义里写 `NOT NULL` 的不重复追加）。
4. 所有 SQLite 连接统一只设 `PRAGMA journal_mode = WAL` 与 `PRAGMA synchronous = NORMAL`；写入按 `BATCH_SIZE` 分桶、逐桶满批提交。
5. 分表口径 `zlib.crc32(product_no) % NUM_TABLES`（`p_no` 为空时取 `"NULL"`，解析器已跳过空主键，属不可达分支）；`INSERT OR REPLACE` + 主键幂等。

## 拉取不变量

- `runtime/state/{AVL}.fetched` 记录已成功入库的远程文件名：每轮先跳过清单命中者，**入库成功后才登记**；失败不登记、本地文件保留、下一轮自动重试。
- `runtime/state/{AVL}.lock` 非阻塞单实例锁，拿不到立即失败；平台无可用锁库时明确报错，不伪装成「已有实例在运行」。
- 所有任务按 `TABLE_PRIMARY_KEYS` 注册的主键 + `INSERT OR REPLACE` 保证幂等：商机回捞 / 控损为 `product_no`，同一手机号只保留一条；诊断维表为各自业务定位键（可复合），同一键只保留最新批次的一条。
- `exists()` 三态：`False` → INFO「远程文件尚未生成」；`FileNotFoundError` → 同 `False`；其他异常 → ERROR「stat 远程文件出错」。后两者都跳过、不登记清单。

## F09633 分表

`F09633`（用户场景对应关系）是诊断 8 表里唯一带 `product_no` 的表，千万级行；
单表全量扫描与写入都吃不消，因此按 `product_no` 分表。

| 项 | 值 |
|---|---|
| 声明 | `avl_specs.AVL_SPECS["F09633"].sharded = True` |
| 分表键 | `product_no`（`conf_user_scene` 第 3 列） |
| 分表数 | `.env` 的 `F09633_NUM_TABLES`（默认 128） |
| 表名 | `F09633_0 … F09633_127` |
| 路由 | `zlib.crc32(str(product_no)) % NUM_TABLES`，`avl_specs.shard_index()` |
| 主键 | `(product_no, scene)`，同一号码恒在同一分表，幂等语义不变 |

**写入侧自动生效**：`build_layout()` 按 `num_tables` 用 `all_targets()` 建满 128 张表，用 `target()` 逐行路由；`ensure_schema()`、`write()`、`purge_expired()` 都不需要为 F09633 改一行代码。在线业务侧按同一物理口径定位分表，其分表数由 `services/.env` 独立声明，改值必须两侧同步并重灌全量。

### 改分表数：重灌全量

分表数来自 `.env` 的 `F09633_NUM_TABLES`。按当前业务口径**不做历史单表兼容**——不存在「旧固定表 → 分表」的迁移路径，改这个值就是重灌：

1. 停掉 F09633 的写入进程；
2. 改本包 `.env` 的 `F09633_NUM_TABLES`（并同步在线侧 `services/.env` 同名键）；
3. 删旧 `F09633.db`（或换 `--runtime-dir`），重跑全量入库：

   ```bash
   python -m sftp_avl2db --task F09633 --date YYYYMMDD
   ```

4. 用 `tests/check_diagnosis_offline.py` 或按真实号码回查确认，再恢复写入进程。

> ⚠ **改 `NUM_TABLES` 等于改「号码 → 分表」映射**：运行中热改会让同一号码按新旧口径落进不同分表，历史数据查不到且是静默降级，必须停写重灌。

> ⚠ **过期清理成本**：`purge_expired()` 会遍历 128 张表按 `op_time < cutoff`
> 做 DELETE，而 `op_time` 不在主键里、没有索引。128 张表各扫一遍相当于全库
> 扫描一轮；千万级数据下请关注 `DIAGNOSIS_POLL_INTERVAL_SECONDS` 与
> `DIAGNOSIS_DATA_EXPIRE_DAYS` 的组合，必要时给 `op_time` 建局部索引或降低
> 清理频率（当前代码未擅自加索引，避免影响全量写入速度）。

## 测试与复跑

```bash
python -m sftp_avl2db --task F09591 --date 20261110 \
  --local-dir io_samples --runtime-dir /tmp/replay
```

`build_task()` 的 `logger=`（可用 `InMemoryLogger`）、`local_dir=`、`day=`、`now=`、`sleeper=`、`batch_size=`、`table_schema=` 都是显式注入点；`runner.serve_forever()` 另有 `max_rounds` / `date_strings` 测试钩子。离线复跑产出隔离到临时目录，不碰包内 `runtime/`。诊断新线 8 个 AVL 的批量复跑命令见上文「诊断新线：8 张表注册」；端到端自检跑 `python tests/check_diagnosis_offline.py`（写入侧配置与在线侧配置独立装配、指向同一批临时库，详见[解耦改造说明](../解耦改造说明.md)）。
