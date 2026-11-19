# local_excel2db · 本地 Excel 维表 → SQLite

把本地 Excel 维表抽列写入 SQLite，写入侧是独立进程，在线业务只读取、不写库。`import local_excel2db` 零 I/O，包自包含（不读 `.env`、项目总 `config.yaml` 或其它包）。**所有相对路径都相对 `registry.json` 所在目录解析**，其它项目用 `--config` 指向自己的配置即可复用。

```
卖点维表.xlsx ─┐
               ├─→ python -m local_excel2db（写入进程）─→ sqlite_db/dimensions.db
标签转换维表.xlsx ┘                                      │ mode=ro
                                                       ↓
                                    main.py / services/（在线只读）
```

## 运行

```bash
python -m local_excel2db                                   # 常驻轮询，间隔由 registry 控制
python -m local_excel2db --once                           # 只检测一轮后退出
python -m local_excel2db --once --force                   # 忽略指纹强制重导并递增 version
python -m local_excel2db --config /path/to/registry.json --interval 300
```

写入进程与 `sftp_avl2db` 的两个拉取任务、在线 API 并列部署；`main.py` 不写业务库/维表。写入侧用 `fcntl` 非阻塞文件锁（`state/dim_watcher.lock`）保证单机单实例；运行时产物 `sqlite_db/`、`state/`、`log/` 均在 registry 所在目录下。

## 注册配置（`registry.json`）

这是唯一的配置入口，`config.py` / `models.py` 在启动时校验，不合法直接抛 `ValueError`，不会按错误配置连库或写错列。

| 层级 | 关键项 | 约束 |
|---|---|---|
| 顶层 | `database` / `poll_interval_seconds` / `version_column` / `meta_table` | 轮询间隔须为正整数；库路径相对 registry 目录 |
| `tables[]` | `table` / `source` / `sheet` / `header_row` / `primary_key` / `columns[]` | 表名、列名合法且不重复；主键必须来自 `columns[].column`，不可重复、不可 `optional` |
| `columns[]` | `source` → `column`、`type`、`default`、`strip`、`optional`、`column_index` | 类型仅 `TEXT` / `INTEGER` / `REAL`；`column_index`（1-based）优先于同名表头；缺必需表头列报 `MissingColumn` |
| `dynamic_columns` | `source_prefix` / `column_prefix` / `start` / `count` | `count` 在 1..200；当前把 `标签0…标签27` 展开为 `tag_0…tag_27`（均 `optional`） |
| `lookup` | `"value"` 或 `"values"` | 二者互斥且必填其一；`value` 返回 `{key: 单值}`，`values` 返回 `{key: (值1, 值2, …)}`，顺序即元组下标；引用列必须已声明且不等于 `key` |
| `cut_in_script` | `key` / `fallback` / `groups` / `tags` / `fallback_key` | 各角色列必须来自 `columns`，`tags` 不可重复，可写 `"dynamic_columns"` 直接引用展开结果；当前 `fallback_key` 为 `"兜底话术"` |

当前注册两张表：

| 表 | 主键 | 读取侧声明 |
|---|---|---|
| `product_selling_point_match` | `business_id` | `lookup.values` → `(product_name, selling_point, label_id)`；28 个 `tag_*` 动态列；`cut_in_script` 角色列 |
| `label_name_mapping` | `label_id` | `lookup.value` → `display_name` |

完整列映射（含 `source` 表头名、`type`、`optional`）以 `registry.json` 为准，改列映射、主键、标签列数后必须删库重建。

## 库表结构与写入流程

每张表 = 配置列 + `version INTEGER NOT NULL` + `PRIMARY KEY (主键列)`；`dim_meta` 以 `table_name` 为主键，记录每表的 `version / source_file / sha256 / file_size / file_mtime / row_count / updated_at`。数据表只保留当前版本，被删行不残留，历史版本不落库。

单轮逐表执行：

1. 文件不存在 → 告警跳过该表，库保留上次成功版本，进程不退出。
2. 算文件 **sha256**，与 `dim_meta` 比对；相同且非 `--force` → SKIP，version 不动。用内容指纹而非 mtime。
3. `openpyxl` 以 `read_only=True, data_only=True` 抽列：按表头名或 `column_index` 定位，`strip` 去首尾空白，`optional` 列缺失填 `default`；缺必需表头列抛 `MissingColumn`，本轮跳过、下轮重试。
4. 主键为空的行跳过，重复主键**后写覆盖先写**。
5. **单事务整表覆盖**：`DELETE FROM 表` → 写入全量行（每行带新 `version`）→ 更新 `dim_meta`；事务内任一环节失败整体回滚，指纹不更新、下轮自动重试。
6. `version` **按表独立**，各自从 1 起，仅在该表文件成功导入时 +1；不同表的 version 数值互不可比。

`ensure_table()` 每次启动比对 `PRAGMA table_info` 与配置列集合，不一致抛 `RuntimeError` 并提示删库重建；**不做自动表结构迁移**。改列映射 / 主键 / 动态列数量后执行：

```bash
rm local_excel2db/sqlite_db/dimensions.db
python -m local_excel2db --once
```

## 读取侧与降级

`DimensionReader` 以 `mode=ro` 打开、用完即关，**在线业务绝不创建或写入维表库**；不做进程级缓存，每次调用读一次全表，天然读到写入侧的最新值。在线侧在请求循环外读 1 次卖点表并显式传参，与产品个数无关。

| API | 返回形状 | 在线消费 |
|---|---|---|
| `load_product_selling_point()`（包级入口） | `{business_id: (product_name, selling_point, label_id)}` | 在线侧唯一使用的维表数据 |
| `DimensionReader.load_lookup(...)` | `{key: 单值}` | 能力保留，当前无在线调用方 |
| `DimensionReader.load_cut_in_script()` | `{business_id: {标签ID / 兜底话术: [切入, 挽留] 或 ""}}` | 能力保留，当前无在线调用方 |

**任何故障（库/表/列不存在、`registry.json` 缺失或损坏、查询异常、未注册表或未声明 lookup / cut_in_script）统一返回 `{}`**，业务按「无维表」降级，维表侧出问题不会让 API 500；同一类告警每进程只打印一次。注册 xlsx 缺失时写入侧告警跳过，库保留上次成功版本。

**库里只存原始单元格文本**：HTML 包装、按 `\n` 拆分、分组对齐等字符串加工全部在读取侧 `transform.py` 完成（`cut_in_str2tuple()` 拆切入/挽留话术，`build_cut_in_script_dict()` 按 `groups` 下标对应 `tag_*` 列、分组数超出时填 `""`、`groups` 为空时整条置 `{}`），不写回库、不固化样式。

## 依赖约定

- 写入侧只需 `openpyxl` 读 xlsx；其余为标准库（`sqlite3` / `hashlib` / `fcntl` / `logging` / `argparse`），不引入 pandas。
- `fcntl` 文件锁为 Linux 单机单实例设计，与部署环境一致。
- 组件通过 `protocols.py` 的 `ReadableRepository` / `WritableRepository` / `InstanceLock` / `LogSink` 契约解耦，可逐层替换。
