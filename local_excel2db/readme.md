# local_excel2db · 用法与结构

> **文档地图**：`local_excel2db/` 的用法说明。
> 同包另一份：[`constraints.md`](constraints.md)（约束）；全局架构与在线业务：[根 `README.md`](../README.md)。
> - [`IOP/readme.md`](../IOP/readme.md) · [`IOP/constraints.md`](../IOP/constraints.md)
> - [`sftp_avl2db/readme.md`](../sftp_avl2db/readme.md) · [`sftp_avl2db/constraints.md`](../sftp_avl2db/constraints.md)

> 本地 Excel 维表 → SQLite，读写分离、可独立复用。`import local_excel2db` 零 I/O；所有相对路径相对 **registry 文件所在目录**解析。本文件讲配置、表结构、写入流程与读取 API；降级与不变量见 `constraints.md`。

## 维表写入侧（Excel → SQLite）

> 维表由独立进程 `python -m local_excel2db` 从 Excel 抽列写入本地 SQLite。
> **库里只存原始单元格文本**，所有字符串加工（HTML 包装、按换行拆分、分组对齐）
> 都在读取侧完成，不写回库、不固化样式；读取与重组见本文「维表数据重组」小节。

## 架构与进程

```
卖点维表.xlsx ─┐
                ├─→ local_excel2db watcher（独立进程，写）─→ local_excel2db/sqlite_db/dimensions.db
标签转换维表.xlsx ┘                                      │
                                                        │ mode=ro
                                                        ↓
                                    main.py / services/（在线业务，只读）
```

| 进程 | 命令（项目根目录） | 职责 |
|---|---|---|
| SFTP 拉取 ×2 | `python -m sftp_avl2db --task xxx` | AVL → `tasks/sqlite_db/` |
| 维表写入 ×1 | `python -m local_excel2db` | xlsx → `dimensions.db` |
| 在线 API ×1 | `python main.py` | 只读业务库与维表；另按阈值写调用量到 `log/call_sum.db`（见根 `README.md` 的「调用量统计」） |

**部署时 4 个进程都要起**。`main.py` 不含任何定时任务，只有一个 `lifespan` 生命周期钩子（见根 `README.md` 的「调用量统计」），除调用量统计外不写业务库/维表；
`local_excel2db` 不依赖 `.env` / `sftp_avl2db/dbspec.py` / `sftp_avl2db/config.py` / 项目总 `config.yaml`，自包含于 `local_excel2db/`；所有相对路径相对 **registry 文件所在目录** 解析，其他项目用 `--config` 指一份配置即可复用。

## 文件清单（`local_excel2db/`）

| 文件 | 作用 |
|---|---|
| `protocols.py` | 组件契约与结果 dataclass（纯 typing，零 I/O） |
| `models.py` | 不可变配置对象 `Registry` / `TableSpec` / `ColumnSpec` / `LookupSpec` / `CutInScriptSpec` |
| `config.py` | `RegistryLoader`：加载并校验 `registry.json`，展开动态列，解析相对路径 |
| `registry.json` | 默认模板：库路径、轮询间隔、文件↔表名↔主键↔列映射、动态列、读取侧角色声明 |
| `excel_source.py` | `OpenpyxlExcelSource`：sha256 指纹、抽列去重、类型转换 |
| `repository.py` | `SqliteDimensionRepository`：建表/结构校验、单事务整表覆盖、`mode=ro` 查询 |
| `importer.py` | `TableImporter`：单表导入编排，产出 `ImportResult` |
| `transform.py` | 纯函数拆解加工 + `CutInScriptTransformer`（只在读取侧执行） |
| `reader.py` | `DimensionReader`：三个只读 API，任何故障降级 `{}` |
| `watcher.py` | `DimensionWatcher` + `SingleInstanceLock` + `build_watcher` 装配工厂 |
| `cli.py` / `__main__.py` / `__init__.py` | 命令行入口、进程退出点、公共 API 与在线业务一行接入 |

读取侧的降级矩阵见 `constraints.md`，重组规则见本文「维表数据重组」；
组件契约可替换：`ReadableRepository` / `WritableRepository` 分开，读取侧替身只需
实现 `query` / `close`，写入侧替身不必实现 `query`。

运行时产物（均在 **registry 文件所在目录** 下，默认即 `local_excel2db/`）：

| 路径 | 内容 |
|---|---|
| `sqlite_db/dimensions.db` | 唯一的维表库 |
| `state/dim_watcher.lock` | `fcntl` 非阻塞锁，保证单机单实例 |
| `log/dim_watcher.log` | 按天轮转（`midnight`），保留 7 天，同时输出到 stderr |

注册文件由 registry 的 `source` 指定；仓库默认模板指向
`../io_samples/卖点维表.xlsx` / `../io_samples/标签转换维表.xlsx`，
clone 后可直接 `python -m local_excel2db --once` 试跑，无需手工复制。
部署时用 `--config /path/to/registry.json` 指自己的配置，xlsx 与库都自动相对该文件解析。

## 注册配置（`registry.json`）

### 顶层

| 配置 | 当前值 | 含义 |
|---|---|---|
| `database` | `sqlite_db/dimensions.db` | 库路径，相对路径解析到 registry 文件所在目录 |
| `poll_interval_seconds` | `600` | 轮询间隔（秒），可被 `--interval` 覆盖 |
| `version_column` | `version` | 版本列名 |
| `meta_table` | `dim_meta` | 版本元数据表名 |

### `tables[]`

| 配置 | 含义 |
|---|---|
| `table` | 英文表名 |
| `source` | 注册文件名（相对 registry 文件所在目录） |
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

### `cut_in_script`（读取侧加工声明）

| 配置 | 当前值 | 含义 |
|---|---|---|
| `key` | `business_id` | 业务 ID 列（查找键） |
| `fallback` | `fallback_script` | 兜底话术原始列 |
| `groups` | `match_result` | 分组 ID 原始列（「匹配结果2」，按 `\n` 拆分） |
| `tags` | `"dynamic_columns"` | 标签话术列的有序数组，或直接引用展开结果；下标 g 对应 `标签{g}` |
| `fallback_key` | `兜底话术` | 生成的嵌套 dict 里兜底项的键名 |

⚠️ `fallback_key` 可配，当前值 `"兜底话术"`；`DimensionReader.load_cut_in_script()`
按它生成兜底项，在线侧已无消费方，调整取值前先确认新线口径。
所有角色列必须来自 `columns` 声明。

### 当前注册的两张表

| 注册文件 | 表名 | 主键 | 固定列（Excel 表头 → 列名） | 动态列 |
|---|---|---|---|---|
| `卖点维表.xlsx` | `product_selling_point_match` | `business_id` | 业务ID→`business_id`、产品名称→`product_name`、生成话术→`selling_point`、标签ID→`label_id`、兜底话术→`fallback_script`、匹配结果2→`match_result`、产品大类→`product_category`、产品小类→`product_subcategory` | `标签0…标签27` → `tag_0…tag_27`（28 列） |
| `标签转换维表.xlsx` | `label_name_mapping` | `label_id` | 标签ID→`label_id`、营销助手显示名字→`display_name`、标签管理平台标签名→`platform_label_name` | 无 |

### 配置校验（`config.py` / `models.py`）

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

## 写入侧流程（`watcher.py` + `importer.py`）

### 运行方式

```bash
python -m local_excel2db                                  # 常驻轮询，间隔由 registry 控制（当前 600s）
python -m local_excel2db --once                          # 只检测一轮后退出（crontab / 验证）
python -m local_excel2db --once --force                  # 忽略指纹强制重导并递增 version
python -m local_excel2db --config /path/to/registry.json --interval 300
```

生产建议 systemd/nohup 起一个常驻进程，与 `sftp_avl2db` 的两个拉取任务并列。
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
> rm local_excel2db/sqlite_db/dimensions.db
> python -m local_excel2db --once
> ```
> 未删库时写入侧每轮报「结构与 registry.json 不一致」，读取侧查询新列失败并
> 降级为空映射（不会 500，但相应维表数据会丢）。不做自动表结构迁移。

### 试跑

```bash
python -m local_excel2db --once                                    # 用包内默认模板（指向 ../io_samples/）
python -m local_excel2db --once --config /path/to/my_registry.json  # 用部署方自己的配置
```

预期：卖点表 1361 行（空主键跳过 187、重复主键覆盖 98）、标签表 60 行，
两表 `version=1`。

## 读取侧（`reader.py` / `DimensionReader`）

三个加载函数，全部 `mode=ro` 打开、用完即关，**在线业务绝不创建/写入维表库**。

| 函数 | 返回形状 | 在线消费方 |
|---|---|---|
| `DimensionReader.load_lookup("label_name_mapping")` | `{label_id: display_name}` | 无（辅助画像不再做名称映射，能力保留供新线复用） |
| `load_product_selling_point()`（包级入口） | `{business_id: (product_name, selling_point, label_id)}` | `main.py` 循环外读入 1 次并显式传参，供 `ProductInfoRaw` 取产品名与非商机 ID 的标签子集 |
| `DimensionReader.load_cut_in_script()` | `{business_id: {标签ID / 兜底话术: [切入, 挽留] 或 ""}}` | 无（维表业务已下线，定义保留供新线复用） |

`DimensionReader.load_cut_in_script()` 用 `ORDER BY rowid` 取
`{key, fallback, groups} + tag_*` 原始列，把每行转成列名映射后交给
`transform.build_cut_in_script_dict()`（由 `CutInScriptTransformer` 收拢列角色）；
列名、标签列顺序、兜底键全部取自 `registry.json`，不硬编码。该方法当前无在线
调用方，留在 `reader.py` 里不产生运行时开销。

## 维表读取侧依赖的 registry 声明

完整配置项与校验见本文「注册配置」；`DimensionReader` 提供以下三类声明的读取
能力，当前在线侧实际只用到 `lookup.values`：

| 声明 | 形状 | 用途 |
|---|---|---|
| `lookup.value` | 单列 → `{key: 单值}` | `label_name_mapping` |
| `lookup.values` | 多列 → `{key: (值1, 值2, 值3)}` | `product_selling_point_match`，`values` 顺序即元组下标 |
| `cut_in_script` | `key` / `fallback` / `groups` / `tags` / `fallback_key` | 声明加工所需的原始列角色，`tags` 可写 `"dynamic_columns"` 直接引用展开结果 |

⚠️ `cut_in_script.fallback_key` 当前值 `"兜底话术"`，由 `CutInScriptTransformer`
按声明生成兜底项；在线侧已无消费方，调整取值前先确认新线的口径。

## 维表数据重组（`transform.py`）

只依赖 `re`，不写回库。库里存的是含换行、`**`、原始 HTML 片段的单元格文本；
`strip()`、`.replace("**", "")`、HTML 包装都在这一步做。

### `cut_in_str2tuple(text)`

把一段话术文本拆成 `[切入话术, 挽留话术]`：

1. 正则 `切入话术(.*?)拒绝挽留话术(.*)`（`re.DOTALL`）拆两段。
2. 每段逐行处理：跳过空行；`:` 统一为 `：` 后按 `：` 切分；
   只有 1 段时原样保留；有 `：` 时用 `^\d+\s?\.\s?` 去掉行首编号，
   把「前缀：」包成 `<strong><span style="color: blue;">前缀：</span></strong>` 再拼回正文。
3. 行间用 `\n` 连接；切入段加 `【切入话术】\n` 前缀，挽留段加 `【挽留话术】\n` 前缀。
4. 匹配不到标记，或两段都为空 → 返回空字符串 `""`（**不是 list**）。

调用方取 `fusion_script_list[0]` 作切入、`[1]` 作挽留；值为 `""` 时 `""[0]`
会 `IndexError`，需由调用方自行兜底。当前在线侧已无该调用方。

### `build_cut_in_script_dict(rows, ...)`

按原始行重建嵌套 dict：

1. 以 `key_col`（`business_id`）为外层键；先算兜底项
   `{fallback_key: cut_in_str2tuple(fallback.strip().replace("**", ""))}`。
2. `groups`（`match_result`）非空 → 按 `\n` 拆成 ID 列表；对每个下标 g：
   `tag_cols[g]` 有内容则 `cut_in_str2tuple(...)` 存入 `inner[one_id]`，否则存 `""`。
   **分组数超过 `tag_cols` 长度时，多出来的分组填 `""`**。
3. `groups` 为空 → **整条置为 `{}`**，兜底项一并丢弃。
4. 重复 `business_id` 按行序后写覆盖。

样例维表有 28 个标签列，个别行拆出 29~30 个分组，规则 2 会生效；
`groups` 为空的行在库里有 403 条，规则 3 会生效。

## 在线业务接入点（维表）

维表业务路由已下线，在线侧不再按 `business_id` 命中卖点表来选业务对象。
当前与维表有关的只剩两处：

| 位置 | 加载函数 | 消费方式 |
|---|---|---|
| `main.py::marketAssistant` | `load_product_selling_point()` | 循环外读入 1 次，作为请求级共享 dict 显式传给三个构造点，不参与路由判断 |
| `services/product_info_raw.py::__init__` | 无（由调用方传入 `selling_points`） | 存 `self.product_selling_point_match_dict`；`[0]` 取产品名（仅用于日志）；商机回捞的 `queried_label_ids` 由代码内固定集提供，其余 ID 才从 `[2].split("\n")` 取 |

`DimensionReader.load_cut_in_script()` 与 `DimensionReader.load_lookup()` 仍由
`reader.py` 提供，但在线侧无调用方；维表库、`registry.json` 与 `transform.py`
的加工能力全部保留，供客户诊断新线复用。
