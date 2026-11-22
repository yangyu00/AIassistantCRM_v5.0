# local_excel2db · 本地 Excel 维表 → SQLite（只写）

**本包只写入**：定时监控项目根 `excels/` 里注册 xlsx 的内容变化，抽列写
SQLite、按表递增 version、记录指纹与元信息。本包**不提供任何在线查询入口**，
也不声明在线业务侧读哪些库、有哪些列 —— 那部分由 `services/db_config.py`
与 `services/.env` 独立维护，两侧唯一的交集是磁盘上的 `dimensions.db` 文件，
没有任何代码 / 配置模块互相引用。

**配置与运行时产出都锚在包内、按 `__file__` 定位，与启动 CWD 无关**。
`import local_excel2db` 零 I/O（不读 registry、不连库、不建目录、不改全局
logger、不拉起 openpyxl / sqlite3）。

```
项目根 excels/卖点维表.xlsx ─┐
                             ├─→ python -m local_excel2db（写入进程）
项目根 excels/标签转换维表.xlsx ┘        │
                                        ↓
                    local_excel2db/runtime/（唯一产出目录）
                      sqlite_db/dimensions.db
                      state/dim_watcher.lock
                      log/dim_watcher.log
```

在线业务侧按自己的配置以 `mode=ro` 只读上面那个 `dimensions.db`；改表名、
列映射或 `tag_*` 个数时，除本包 `registry.json` 外还要同步
`services/db_config.py` 的卖点维表声明，详见 [`services/README.md`](../services/README.md)「职责边界」。

## 路径边界（代码强制，不靠约定）

| 类别 | 位置 | 谁决定 |
|---|---|---|
| 配置 | `local_excel2db/registry.json` | `config.REGISTRY_PATH`，按 `__file__` 定位；**本包唯一来源** |
| 输入 | 项目根 `excels/` | `config.INPUT_DIR = PROJECT_ROOT / "excels"`；`source` 只允许纯文件名 |
| 库 | `local_excel2db/runtime/sqlite_db/dimensions.db` | `config.DB_PATH` |
| 单实例锁 | `local_excel2db/runtime/state/dim_watcher.lock` | `config.LOCK_PATH` |
| 日志 | `local_excel2db/runtime/log/dim_watcher.log` | `config.LOG_DIR` |

- `registry.json` 里**不写库路径**，产物目录是代码常量，配置改不动它。
- `source` 只能是 `卖点维表.xlsx` 这样的纯文件名，加载时强制拼到 `INPUT_DIR`；
  写 `../`、子目录或绝对路径直接抛 `ValueError`，保证永远只扫根 `excels/`。
- 需要把数据挂到独立盘，可把 `runtime/` 做成软链。
  根目录 `.gitignore` 已忽略 `local_excel2db/runtime/*`（保留 `.gitkeep`）。

## 运行

```bash
python -m local_excel2db                 # 常驻轮询，间隔由包内 registry 控制（600s）
python -m local_excel2db --once         # 只检测一轮后退出（crontab / 验证）
python -m local_excel2db --once --force # 忽略指纹强制重导并递增 version
```

单机单实例由 `fcntl` 非阻塞文件锁保证，锁冲突退出码 1，
配置损坏退出码 2。改轮询间隔或列映射后**重启写入进程**生效。

## 配置（`registry.json`，本包唯一来源、唯一入口 `load_registry()`）

| 层级 | 关键项 | 约束 |
|---|---|---|
| 顶层 | `poll_interval_seconds` / `version_column` / `meta_table` | 轮询间隔须为正整数；表名、列名走 `_NAME_RE` 白名单 |
| `tables[]` | `table` / `source` / `sheet` / `header_row` / `primary_key` / `columns[]` | 表名、列名合法且不重复；主键必须来自 `columns[].column`，不可重复、不可 `optional` |
| `columns[]` | `source` → `column`、`type`、`default`、`strip`、`optional`、`column_index` | 类型仅 `TEXT` / `INTEGER` / `REAL`；`column_index`（1-based）优先于同名表头；缺必需表头列报 `MissingColumn` |
| `dynamic_columns` | `source_prefix` / `column_prefix` / `start` / `count` | `count` 在 1..200；当前把 `标签0…标签27` 展开为 `tag_0…tag_27`（均 `optional`） |

当前注册两张表：

| 表 | 主键 | 摘要 |
|---|---|---|
| `selling_points` | `product_id` | 8 个静态列 + 28 个 `tag_*` 动态列；`source` 表头仍为「业务ID」 |
| `label_name_mapping` | `label_id` | 3 列 |

完整表名与列映射以 `registry.json` 为准。改表名、列映射、主键、标签列数后必须删库重建，并同步在线侧 `services/db_config.py` 的 `SELLING_POINTS_*` 声明：

```bash
rm local_excel2db/runtime/sqlite_db/dimensions.db
python -m local_excel2db --once
```

## 库表结构与写入流程

每张表 = 配置列 + `version INTEGER NOT NULL` + `PRIMARY KEY (主键列)`；
`dim_meta` 以 `table_name` 为主键，记录每表的
`version / source_file / sha256 / file_size / file_mtime / row_count / updated_at`。
数据表只保留当前版本，被删行不残留，历史版本不落库。

单轮逐表执行：

1. 文件不存在 → 告警跳过该表，库保留上次成功版本，进程不退出。
2. 算文件 **sha256**，与 `dim_meta` 比对；相同且非 `--force` → SKIP，version 不动。
   用内容指纹而非 mtime。
3. `openpyxl` 以 `read_only=True, data_only=True` 抽列：按表头名或 `column_index`
   定位，`strip` 去首尾空白，`optional` 列缺失填 `default`；缺必需表头列抛
   `MissingColumn`，本轮跳过、下轮重试。
4. 主键为空的行跳过，重复主键**后写覆盖先写**。
5. **单事务整表覆盖**：`DELETE FROM 表` → 写入全量行（每行带新 `version`）→
   更新 `dim_meta`；事务内任一环节失败整体回滚，指纹不更新、下轮自动重试。
6. `version` **按表独立**，各自从 1 起，仅在该表文件成功导入时 +1；不同表的
   version 数值互不可比。

`ensure_table()` 每次启动比对 `PRAGMA table_info` 与配置列集合，不一致抛
`SchemaMismatch`（`RuntimeError` 子类，定义在 `protocols.py`）并提示删库重建；
**不做自动表结构迁移**。库里只存原始单元格文本，不做任何 HTML 包装或话术拆分。

## 容错与日志

| 触发 | 行为 |
|---|---|
| 注册 xlsx 缺失 | 告警跳过该表，保留上次成功版本 |
| 缺必需表头列 / 读文件异常 | 本表本轮跳过，指纹不更新，下轮重试；不影响其它表 |
| 库表结构与 registry 漂移 | 抛 `SchemaMismatch`，提示删库重建，不静默写错列 |
| 已有实例持锁 | 打 ERROR 后退出码 1，不并发写库 |
| 配置损坏 / 缺失 | 启动阶段返回码 2，不按脏配置连库 |

日志按天轮转保留 7 天，同时输出到 stderr；路径恒为 `runtime/log/dim_watcher.log`。

## 依赖约定

- 写入侧只需 `openpyxl` 读 xlsx；其余为标准库（`sqlite3` / `hashlib` / `fcntl` /
  `logging` / `argparse`），不引入 pandas。
- `fcntl` 文件锁为 Linux 单机单实例设计，与部署环境一致。
- 组件通过 `protocols.py` 的 `ExcelSource` / `WritableRepository` / `InstanceLock` /
  `LogSink` / `Sleeper` 契约解耦，可逐层替换；注入只替换组件，不改任何路径。
