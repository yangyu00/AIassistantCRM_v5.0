# io_samples/ · 输入 / 输出样例

这里放的是**随仓库提交的样例文件**，用于离线复跑、解析调试和对照格式；
不是生产数据目录，在线进程不会主动读它。

> **一句话先说清楚**：两张 `*.xlsx` 只是格式样例。
> **`local_excel2db` 实际要处理的是 `excels/` 文件夹**（部署环境里的维表投放目录），
> 日常更新维表改 `excels/` 里的文件，不要改本目录里的样例。详见下方「维表 Excel 的实际目录」。

## 文件清单

| 文件 | 归属模块 | 用途 |
|---|---|---|
| `F0955420261110000000.AVL` | `sftp_avl2db` · 商机回捞 | 号卡业务样例，2 行记录 |
| `F0955520261110000000.AVL` | `sftp_avl2db` · 商机回捞 | 套餐业务样例，2 行记录 |
| `F0955620261110000000.AVL` | `sftp_avl2db` · 商机回捞 | 宽带业务样例，2 行记录 |
| `F0959120261110000000.AVL` | `sftp_avl2db` · 控损 | 控损样例，40 行；含 `<$B>` / `<$I>` / `<$U>` / `<#RRGGBB>` 自定义标签，供 `rewrite_by_spec_f09591` 的 HTML 转换对照 |
| `iop_labels_query_raw_response.json` | `IOP` | 标签查询接口的原始响应样例：`res_code` / `res_desc` / `result.BUSIINFO[]`，每项含 `ITEMCODE` / `ITEMNAME` / `ITEMVALUE` |
| `卖点维表.xlsx` | `local_excel2db` | `product_selling_point_match` 表的格式样例（业务 ID、话术、标签 ID、`标签0…标签27` 等） |
| `标签转换维表.xlsx` | `local_excel2db` | `label_name_mapping` 表的格式样例，取名为「标签名称--标签ID」的 sheet |

AVL 命名遵循 `{业务前缀}{YYYYMMDD}000000.AVL`，制表符分隔、UTF-8；
四个文件名的日期部分都是 `20261110`。字段含义与 AVL 映射见
[`sftp_avl2db/README.md`](../sftp_avl2db/README.md) 与 `sftp_avl2db/avl_specs.py`、
`sftp_avl2db/table_schemas.py`。

## 三个子系统分别怎么用这些样例

**1. SFTP / AVL 离线复跑**（只吃 `*.AVL`）

```bash
python -m sftp_avl2db --task F09591 --date 20261110 \
  --local-dir io_samples --runtime-dir /tmp/replay
```

`--local-dir io_samples` 让拉取任务把本目录当成本地 AVL 源，`--runtime-dir` 把
产物隔离到临时目录，不碰包内 `sftp_avl2db/runtime/`。生产环境里 AVL 落在
`sftp_avl2db/runtime/files/{项目名}/`，输出的 `{AVL}.db` 落在
`sftp_avl2db/runtime/sqlite_db/{项目名}/`，与本目录无关。

**2. IOP 响应对照**（只吃那个 `.json`）

`iop_labels_query_raw_response.json` 是排查字段口径时看的原始报文形状；
在线链路里 `IOPClient` 会把它解析成 `{ITEMCODE: (名称, 值)}` 的画像 dict，
不做打桩、不读这个文件。想验证真实上游用
[`tests/check_iop_direct.py`](../tests/README.md)。

**3. 维表 Excel**（只吃两个 `.xlsx`，且见下节）

`local_excel2db` 按 `registry.json` 中每张表的 `source` 轮询 xlsx、抽列写入
`local_excel2db/sqlite_db/dimensions.db`，在线侧只读这个库。

## 维表 Excel 的实际目录：`excels/`

| 目录 | 角色 | 谁读它 |
|---|---|---|
| `io_samples/` | 随仓库提交的**格式样例**，用于开箱即跑和对照 | 默认 `registry.json` 的两处 `source`（当前指向这里） |
| **`excels/`** | **`local_excel2db` 实际要处理的维表投放目录** | 生产写入进程按 `registry.json` 的 `source` 轮询 |
| `local_excel2db/sqlite_db/` | 导入产物 `dimensions.db` | 在线侧 `mode=ro` 只读 |

**为什么容易搞混**：仓库自带的 `local_excel2db/registry.json` 为了让 clone 下来
就能跑 `--once`，把两张表的 `source` 写成了 `../io_samples/卖点维表.xlsx` 和
`../io_samples/标签转换维表.xlsx`。这是**样例口径**；实际部署时维表统一投放到
`excels/`，把两处 `source` 改成 `../excels/卖点维表.xlsx` /
`../excels/标签转换维表.xlsx` 即可，或者用 `--config` 指一份自己的 registry：

```bash
python -m local_excel2db --config /path/to/registry.json --once
```

路径基准是 **registry.json 所在目录**（`config.py::_resolve`），与启动时的 CWD
无关，所以也支持写绝对路径。配置说明见 [`local_excel2db/README.md`](../local_excel2db/README.md)。

两个实操提醒：

- 用仓库默认配置跑 `python -m local_excel2db --once` 会把**本目录的样例**导入
  `dimensions.db`。本地调试没问题，但别把这份样例库当成生产维表库用；改列映射
  或主键后要删库重建。
- `excels/` 里是真实维表，通常不随仓库提交；如果确实放在工作区内，记得按需
  加进 `.gitignore`，别把客户/产品数据提交上去。

## 数据与脱敏

AVL 样例里含手机号、通话总结、拒绝原因等话术文本，JSON 样例里含
`ITEMVALUE`，两张 xlsx 含产品名与话术——都是**脱敏后的样例数据**，不代表真实
业务内容。往这里补新样例前先确认无真实客户信息；生产数据一律走
`sftp_avl2db/runtime/`、`excels/` 等运行时目录，项目 `.gitignore` 已忽略
`*.db`、`*.log`、`runtime/` 等产物。

`tests/` 里的手工冒烟脚本打的是运行中的服务，不使用这些样例文件。
