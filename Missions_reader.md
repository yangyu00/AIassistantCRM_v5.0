# 在线业务读取与重组说明（FastAPI）

> 写入侧（SFTP 拉取 3 个进程、维表写入 1 个进程）见 `Missions_writer.md`。
> 在线 API（`python main.py`）不写任何库、不含 startup 定时任务，
> 所有业务数据与维表一律只读 SQLite。

## 架构与数据流向

```
tasks/sqlite_db/{任务名}/*.db ──→ sftp_workdir_*/data_query.py ──┐
                                                                ├─→ main.py（路由）→ services/（重组）→ output_script
dimensions.db ──→ dim_reader.py ──→ dim_transform ──────────────┘
```

| 进程 | 数量 | 职责 |
|---|---|---|
| 写入侧进程 | 4 | 3 个 SFTP 拉取 + 1 个 `dim_watcher`，见 `Missions_writer.md` |
| 在线 API | 1 | 只读两类 SQLite 库，按路由选业务对象并拼装话术 |

**部署时 5 个进程都要起**。库未就绪时 API 按「无业务记录 / 无维表」降级，不会 500。
维表更新后无需重启在线进程，下一次请求即读到新值。

## 请求处理与路由（`main.py::marketAssistant`）

1. `IOP.get_custinfo` 取用户标签 `custom_label_info`，异常时置 `{}`。
2. 按 `productId` 列表逐个建对象，命中即 `continue`（顺序不可调换）：

| 顺序 | 条件 | 数据源 | 业务对象 |
|---|---|---|---|
| 1 | 202608041615335173 / 172 / 174 | `data_query_f09554.get_latest_record(telnum, product_id)` | `ProductInfoBySpec9554` |
| 2 | 202609180955095402 | `data_query_f09591.get_latest_record(telnum)`，有记录才命中 | `ProductInfoBySpec9591` |
| 3 | 其余 ID | `data_query_f09609.get_record(telnum, product_id)`，有记录才命中 | `ProductInfoBySpec9609` |
| 4 | `load_product_selling_point()` 命中 business_id | 维表库 | `ProductInfoByExcel` |
| 5 | 兜底 | 维表库 | `ProductInfoRaw` |

3. 输出：`product_name` 非空 → `code: "0"` 带话术；为空 → `code: "2"`、产品名与话术为空。

## 业务库读取（`sftp_workdir_*/data_query.py`）

库目录由 `tasks/config.py` 的 `db_dir(任务名)` 统一指向 `tasks/sqlite_db/{任务名}/`，
与写入侧路径一致。三个模块均为 `async` 函数、内部同步 `sqlite3` 查询，
列名与任务脚本的 `DB_SCHEMA` 对齐；库文件不存在、未映射、查询异常都返回 `{}`。

| 模块 | 函数 | 定位方式 | 查询 | 返回字段 |
|---|---|---|---|---|
| `sftp_workdir_f09554` | `get_latest_record(telnum, product_id)` | `PRODUCT_TABLE_MAP` 映射 3 个 ID → `data_F09554/55/56`，各一个 `.db` | `WHERE product_no = ? ORDER BY op_time DESC LIMIT 1` | 12 列（op_time…city_id） |
| `sftp_workdir_f09591` | `get_latest_record(product_no)` | `crc32(product_no) % 128` → `data_F09591_N`，单库 `F09591.db` | `WHERE product_no = ? ORDER BY op_time DESC LIMIT 1` | 5 列（op_time, user_id, product_no, city_id, strategy_name） |
| `sftp_workdir_f09609` | `get_record(product_no, product_id)` | `crc32(product_no) % 128` → `data_F09609_N`，单库 `F09609.db` | `WHERE product_no = ? AND product_id = ? LIMIT 1` | 5 列（op_time, user_id, product_no, product_id, strategy_script） |

主键、分表数与库表定义的完整口径见 `Missions_writer.md`。

## 业务库话术重组（3 个 spec 子类）

| 子类 | 读取字段 | 重组规则 |
|---|---|---|
| `rewrite_by_spec_f09554` | `content_abstract` / `type_xl` / `cust_demand` / `rec_strategy` / `reject_reason` / `reason_note` | 拼【通话总结】【用户需求】【已推荐政策】【拒绝原因】四段，标题加粗、部分蓝色，缺值写「无」；末尾拼 `auxiliary_portrait`，换行统一替换为 `<br>` |
| `rewrite_by_spec_f09591` | `strategy_name` | 自定义标签 `<$B>` / `<$I>` / `<$U>` / `<#RRGGBB>` / `<$B$I#RRGGBB>` → `<b>` / `<i>` / `<u>` / `<span style="color: #RRGGBB">`，`</>` 按栈逆序闭合；空内容返回「无历史话术记录」 |
| `rewrite_by_spec_f09609` | `strategy_script` | 按 `\n` 拆行、按 `[：:]` 切一次，「标题：」蓝色加粗；标题为「资费摘要」时标题与内容均红色加粗；换行替换为 `<br>`；空内容返回「无话术记录」 |

这些格式化都在业务侧执行，库里保存的是原始文本。

## 读取侧（`dim_reader`）

三个加载函数，全部 `mode=ro` 打开、用完即关，**在线业务绝不创建/写入维表库**。

| 函数 | 返回形状 | 消费方 |
|---|---|---|
| `load_lookup("label_name_mapping")` | `{label_id: display_name}` | `ProductInfoRaw` 辅助画像 |
| `load_product_selling_point()` | `{business_id: (product_name, selling_point, label_id)}` | `main.py` 路由、`ProductInfoRaw`、`ProductInfoByExcel` |
| `load_cut_in_script()` | `{business_id: {标签ID / 兜底话术: [切入, 挽留] 或 ""}}` | `ProductInfoByExcel` |

`load_cut_in_script()` 用 `ORDER BY rowid` 取
`{key, fallback, groups} + tag_*` 原始列，把每行转成列名映射后交给
`dim_transform.build_cut_in_script_dict()`；列名、标签列顺序、兜底键全部取自
`registry.json`，不硬编码。

### 降级矩阵

| 场景 | 行为 |
|---|---|
| 库文件不存在 | 返回 `{}` |
| 表不存在 / 列缺失（如缺 `tag_*`） | 返回 `{}`，提示删库重建 |
| 未注册表 / 未声明 `lookup` / `cut_in_script` | 返回 `{}` |
| 查询异常、`registry.json` 缺失或损坏 | 返回 `{}` |
| 注册 xlsx 缺失 | 写入侧告警跳过，库保留上次成功版本 |

返回 `{}` 时业务行为：产品名为 `None`、`queried_label_ids` 为 `[]`、
卖点/切入/挽留话术为空、辅助画像的标签名沿用上游 `custom_label_info` 给的名字。
**维表侧出任何问题都不会让 API 500**。同一类告警每进程只打印一次（`_WARNED` 集合），避免刷屏。

### 读取次数

不做进程级缓存，每次调用读一次全表，天然读到 `dim_watcher` 写入的最新值，
无缓存失效或脏读问题。一次请求内：

- `main.py` 的 `marketAssistant` 循环**外**加载 1 次，供「维表中业务」路由判断复用；
- 每个 `ProductInfoRaw` 实例加载 1 次卖点表 + 1 次标签映射表（循环外各一次）；
- `ProductInfoByExcel` 复用父类那份卖点表，另加载 1 次 `cut_in_script`（36 列原始列）。

标签映射表 60 行、卖点表 1361 行，本地 SQLite 开销可忽略。
后续若要省连接，可基于**各表独立的 version** 加缓存。

## 维表读取侧依赖的 registry 声明

完整配置项与校验见 `Missions_writer.md`；在线读取只消费其中两类声明：

| 声明 | 形状 | 用途 |
|---|---|---|
| `lookup.value` | 单列 → `{key: 单值}` | `label_name_mapping` |
| `lookup.values` | 多列 → `{key: (值1, 值2, 值3)}` | `product_selling_point_match`，`values` 顺序即元组下标 |
| `cut_in_script` | `key` / `fallback` / `groups` / `tags` / `fallback_key` | 声明加工所需的原始列角色，`tags` 可写 `"dynamic_columns"` 直接引用展开结果 |

⚠️ `cut_in_script.fallback_key` 可配，但 `services/rewrite_by_excel/rewrite.py` 用
`"兜底话术"` 做比较，改它需同步改业务代码。

## 维表数据重组（`dim_transform`）

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

业务侧 `fusion_script_list[0]` 取切入、`[1]` 取挽留；值为 `""` 时 `""[0]`
会 `IndexError` 并被业务侧 `except` 吞掉返回空串。

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

| 位置 | 加载函数 | 消费方式 |
|---|---|---|
| `main.py::marketAssistant` | `load_product_selling_point()` | `if one_product_obj.id in dict` 判断是否走「维表中业务」分支 |
| `services/product_info_raw.py::__init__` | 同上，存 `self.product_selling_point_match_dict` | `[0]` 取产品名；`[2].split("\n")` 取 `queried_label_ids`（不提前拆、不过滤空串、不额外 strip 每项） |
| `services/product_info_raw.py::__get_auxiliary_portrait` | `load_lookup("label_name_mapping")` | 命中则替换辅助画像的标签名，后续所有标签特殊规则不变 |
| `services/rewrite_by_excel/rewrite.py::__init__` | `load_cut_in_script()`，存 `self.cut_in_script_dict` | 切入取 `[0]`、挽留取 `[1]` |
| `services/rewrite_by_excel/rewrite.py::__get_selling_point` | 复用父类 `self.product_selling_point_match_dict` 的 `[1]` | 正则 `\d、.*?：` 找编号段，首段包 `<strong>` + `<span style="color:red;">`，后续段落逐段 `<strong>` 拼接补换行；匹配不到编号只加 `【卖点】\n`。输出再经 `__strong_text()` 把四个小节标题包 `<strong>` |

**格式转换全部留在业务侧**，库里 `selling_point` 存原始「生成话术」文本，
以后改配色/标签不用重导全表。

## 公共画像拼装（`services/product_info_raw.py`）

所有业务对象都继承 `ProductInfoRaw`，构造时固定走这段重组：

- `queried_label_ids`：卖点维表元组的 `[2]`，业务侧 `.split("\n")` 得到
  （读取侧不提前拆、不过滤空串、不额外 strip 每项，保证与画像逐字一致）。
- `queried_label_nvs`：按 `queried_label_ids` 顺序与 `custom_label_info` 对齐，缺失填 `None`。
- `auxiliary_portrait`：逐个标签拼 `标签名: <strong><span style="color:blue;">值</span></strong>; `，
  命中 `label_name_mapping` 时替换标签名；随后应用一组按标签 ID 硬编码的特殊规则
  （掉线记录 0.00 → 「无」、费用类补「元」、抖音快手流量补「M」、1/0 → 是/否、
  重复标签去重、降档阈值标题标红），最后补 `【辅助画像】` 前缀。

`ProductInfoByExcel` 在此基础上追加卖点、切入、挽留三段，经 `__strong_text()`
把四个小节标题包成 `<strong>` 后作为 `output_script`。

## 降级与容错汇总

| 侧 | 触发 | 行为 |
|---|---|---|
| 维表 | 库/表/列不存在、`registry.json` 异常、查询失败 | `dim_reader` 返回 `{}`，业务按「无维表」降级 |
| 业务库 | 库文件不存在、product_id 未映射、查询异常 | `data_query` 返回 `{}`，对应 spec 子类走空记录分支 |
| 写入侧 | 注册 xlsx 缺失 | `dim_watcher` 告警跳过，库保留上次成功版本 |

**维表侧出任何问题都不会让 API 500**：产品名为 `None`、`queried_label_ids` 为 `[]`、
卖点/切入/挽留话术为空、辅助画像标签名沿用上游给的名字。
同一类告警每进程只打印一次（`_WARNED` 集合），避免刷屏。

不做进程级缓存，每次调用读一次全表，天然读到写入侧的最新值，
无缓存失效或脏读问题；改库后下一次请求即生效，无需重启。

## 自检要点（在线侧）

- 删库后调用 `load_product_selling_point()` / `load_cut_in_script()` 应返回 `{}`
  且不创建任何文件。
- `import main` 正常，`app.router.on_startup` 为空（无定时任务）。
- 只改卖点表中的 `product_name` 后直接请求接口，下一次调用即读到新名字。
- 三个 `data_query` 模块、三个 spec 子类、`dim_reader` 均可独立导入。

## 未实现

- 基于 `version` 的进程级缓存、版本回滚、历史版本查询、灰度切换。
- 加工规则的完全配置化：当前 `cut_in_str2tuple` 与分组规则写在 `dim_transform.py`，
  `registry.json` 只声明列角色与列顺序。
