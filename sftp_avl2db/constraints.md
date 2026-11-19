# sftp_avl2db · 约束、降级与自检

> **文档地图**：`sftp_avl2db/` 的约束说明。
> 同包另一份：[`readme.md`](readme.md)（用法）；全局架构与在线业务：[根 `README.md`](../README.md)。
> - [`IOP/readme.md`](../IOP/readme.md) · [`IOP/constraints.md`](../IOP/constraints.md)
> - [`local_excel2db/readme.md`](../local_excel2db/readme.md) · [`local_excel2db/constraints.md`](../local_excel2db/constraints.md)

> 写入侧不变量：不重复拉取、失败重试、跨天兜底、单实例锁、入库幂等，以及业务库降级与自检。用法与配置见 `readme.md`。

## 通用规则（不变量）

> 条目编号沿用原「通用规则」；用法类条目（1、6–9）见 `readme.md`。

2. **不重复拉取**：每个任务维护 `tasks/state/{任务名}.fetched`，记录已成功入库的远程文件名；
   每轮先跳过清单里的文件，**入库成功后才登记**。本地已有同名 `.AVL` 不会跳过，
   清单未命中时会重新下载并覆盖（`--force-reload` 则连清单命中也强制重下）。
3. **失败重试**：入库失败不登记清单，下一轮自动重试；本地文件保留。
4. **跨天兜底**：每轮同时检查「昨天 + 今天」两天的文件名。
   > ⚠️ **文件名日期 ≠ 数据日期（op_time）**：`{YYYYMMDD}` 是文件生成日，`op_time` 是业务数据日，各任务偏移量见 `readme.md` 的「文件日期与数据日期」。
   > 补跑时 `--date` 填**文件名日期**：补 op_time=2026-11-08 的数据，回捞商机用 `--date 20261110`，控损用 `--date 20261109`。
5. **单实例锁**：`tasks/state/{任务名}.lock`（portalocker/fcntl 非阻塞锁）。

## 入库幂等

- **回捞商机、控损**：`PRIMARY KEY (product_no)` + `INSERT OR REPLACE`，同一 `product_no` 只保留一条。
- `product_no` 为 `TEXT NOT NULL`；空手机号记录在解析时跳过，跳过条数写进日志。
- 主键自带索引，不再额外建 `(product_no, op_time)` 索引。
- `REPLACE` 是后写覆盖先写。批量补跑历史文件按文件名日期**升序**执行（先旧后新）；
  常驻轮询按「昨天 → 今天」跑，天然符合。
- 在线查询的表名、列名、分表路由键由 `services/data_query.py` 从同一份
  `PROFILES[任务名]` / `sftp_avl2db.dbspec.DB_SCHEMAS` / `QUERY_SPECS` 派生，库路径见 `readme.md` 的「配置」小节。

## 降级与容错（业务库）

| 侧 | 触发 | 行为 |
|---|---|---|
| 业务库 | 库文件不存在、`product_id` 未映射、未注册任务名、查询异常 | `services/data_query.py` 返回 `{}`，对应 spec 子类走空记录分支 |

## 自检要点（写入侧 / 库表契约）

- `PROFILES` / `DB_SCHEMAS` / `QUERY_SPECS` 三者 key 一致；改 `.env` 的
  `LOSS_CONTROL_NUM_TABLES` 后，`get_loss_control_latest()` 解析出的表名后缀同步变化，
  写入侧 `Crc32ShardLayout` 的分表数也应同步。
- `QUERY_MODES` 恰为三种；`_resolve()` 对两个任务分别解析出
  `{库名}.db` + `data_{库名}`、`F09591.db` + `data_F09591_N`。
  把 `mode` 改成多库多表等非法值，调用 `sftp_avl2db.dbspec.validate_specs(load_profiles())` 应抛 `ValueError`。
- `import sftp_avl2db` 不读 `.env`、不加载 paramiko / loguru；
  `python -m sftp_avl2db --help` 在空目录下可运行。

> 客户诊断新线等跨模块「未实现」项见 [根 `README.md`](../README.md)。
