# local_excel2db · 约束、降级与自检

> **文档地图**：`local_excel2db/` 的约束说明。
> 同包另一份：[`readme.md`](readme.md)（用法）；全局架构与在线业务：[根 `README.md`](../README.md)。
> - [`IOP/readme.md`](../IOP/readme.md) · [`IOP/constraints.md`](../IOP/constraints.md)
> - [`sftp_avl2db/readme.md`](../sftp_avl2db/readme.md) · [`sftp_avl2db/constraints.md`](../sftp_avl2db/constraints.md)

> 写入侧与读取侧的不变量：`mode=ro`、故障降级 `{}`、每请求读 1 次不缓存、单事务整表覆盖、按表独立 version、表结构变更需删库重建。配置与流程见 `readme.md`。

## 依赖与约定

- 写入侧只需 `openpyxl`（读 xlsx）；其余为标准库
  （`sqlite3` / `hashlib` / `fcntl` / `logging` / `argparse`）。
  不引入 pandas / yapf，不读 `.env` 或项目总 `config.yaml`。
- `fcntl` 文件锁为 Linux 单机单实例设计，与项目部署环境一致。
- `local_excel2db/sqlite_db/`、`state/`、`log/` 是运行时产物，建议加入 `.gitignore`。

## 降级矩阵

| 场景 | 行为 |
|---|---|
| 库文件不存在 | 返回 `{}` |
| 表不存在 / 列缺失（如缺 `tag_*`） | 返回 `{}`，提示删库重建 |
| 未注册表 / 未声明 `lookup` / `cut_in_script` | 返回 `{}` |
| 查询异常、`registry.json` 缺失或损坏 | 返回 `{}` |
| 注册 xlsx 缺失 | 写入侧告警跳过，库保留上次成功版本 |

返回 `{}` 时业务行为：`ProductInfoRaw` 的产品名为 `None`、其余 ID 的
`queried_label_ids` 为 `[]`；商机回捞的标签子集是代码内固定值，辅助画像由
IOP 标签拼装、不受维表影响；控损与诊断兜底不计算画像；兜底统一返回
`code: "2"` 空话术。
**维表侧出任何问题都不会让 API 500**。同一类告警每进程只打印一次（`DimensionReader._warned` 类级集合），避免刷屏。

## 读取次数

不做进程级缓存，每次调用读一次全表，天然读到写入侧进程写入的最新值，
无缓存失效或脏读问题。一次请求内：

- `main.py::marketAssistant` 在循环**外**读 1 次卖点表，把同一份 dict
  显式传给每个产品对象；三个构造点都接收该参数，**每请求恰好 1 次**；
- `label_name_mapping` 与 `cut_in_script` 在线侧均不再加载。

卖点表 1361 行，本地 SQLite 开销可忽略。
后续若要省连接，可基于**各表独立的 version** 加缓存。

## 降级与容错汇总（维表 / 写入侧）

| 侧 | 触发 | 行为 |
|---|---|---|
| 维表 | 库/表/列不存在、`registry.json` 异常、查询失败 | `DimensionReader` 返回 `{}`，业务按「无维表」降级 |
| 写入侧 | 注册 xlsx 缺失 | `python -m local_excel2db` 告警跳过，库保留上次成功版本 |

## 维表故障对在线业务的影响

**维表侧出任何问题都不会让 API 500**：`ProductInfoRaw` 的产品名为 `None`、
商机回捞的标签子集仍取代码内固定值、其余 ID 的 `queried_label_ids` 为 `[]`，
兜底统一 `code: "2"` 空话术；商机辅助画像只由 IOP 标签决定，不受维表影响，
控损与兜底不计算画像。同一类告警每进程只打印一次（`DimensionReader._warned` 类级集合），避免刷屏。

不做进程级缓存，每次调用读一次全表，天然读到写入侧的最新值，
无缓存失效或脏读问题；改库后下一次请求即生效，无需重启。

## 自检要点（写入侧）

- `python -m local_excel2db --once` 后 `dim_meta` 两行各自 `version=1`、`row_count` 与日志一致。
- 再跑一次：两表都 SKIP、version 不变；改其中一个文件：只有对应表的 version +1。

## 自检要点（维表）

- 删库后调用 `load_product_selling_point()` / `DimensionReader.load_cut_in_script()`
  应返回 `{}` 且不创建任何文件。

- 商机回捞的 `queried_label_ids` 是代码内固定集，改卖点维表的 `label_id` 列
  不影响画像；`label_name_mapping` 与 `cut_in_script` 在线侧不再被加载。

- **维表读取次数**：打桩 `load_product_selling_point()`，一次请求应为 **1** 次，
  与列表里产品个数无关；三个构造点都显式接收同一份 dict。
- `services/data_query.py`、两个 spec 子类、`local_excel2db.reader` 均可独立导入；
  `import local_excel2db` 零 I/O、不拉起 openpyxl / sqlite3。

## 未实现（写入侧）

- 数据表只保留当前版本；版本回滚、历史版本查询、灰度切换未做。
- 不做自动表结构迁移，列集合变化需人工删库重建。

## 未实现（维表）

- 基于 `version` 的进程级缓存、版本回滚、历史版本查询、灰度切换。
- 加工规则的完全配置化：当前 `cut_in_str2tuple` 与分组规则写在 `transform.py`，
  `registry.json` 只声明列角色与列顺序。
