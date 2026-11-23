# io_samples/ · 输入 / 输出样例

随仓库提交的样例文件，用于离线复跑、解析调试和对照格式；不是生产数据目录，在线进程不会主动读它。两张 `*.xlsx` 只是格式样例，`local_excel2db` 唯一输入是项目根 `excels/`。

## 1. 文件清单

| 文件 | 归属模块 | 用途 |
|---|---|---|
| `F0955420261110000000.AVL` | `sftp_avl2db` | 号卡业务样例，2 行记录 |
| `F0955520261110000000.AVL` | `sftp_avl2db` | 套餐业务样例，2 行记录 |
| `F0955620261110000000.AVL` | `sftp_avl2db` | 宽带业务样例，2 行记录 |
| `F0959120261110000000.AVL` | `sftp_avl2db` | 控损样例，40 行；含 `<$B>` / `</>` / `<#RRGGBB>`（3 种颜色）自定义标签，供 `services/loss_control` 的 HTML 转换对照；解析器另支持 `<$I>` / `<$U>` 与组合标签，样例未覆盖 |
| `F0962720261120000000.AVL` | `sftp_avl2db` | `SCENE_RANKE` 样例，10 行 |
| `F0962820261120000000.AVL` | `sftp_avl2db` | `PRODUCT_SCENE` 样例，10 行 |
| `F0962920261120000000.AVL` | `sftp_avl2db` | `PRODUCT_BUSINESS` 样例，10 行 |
| `F0963020261120000000.AVL` | `sftp_avl2db` | `SCENE_QRHUASHU` 样例，10 行 |
| `F0963120261120000000.AVL` | `sftp_avl2db` | `PRODUCT_YXHUASHU` 样例，10 行 |
| `F0963220261120000000.AVL` | `sftp_avl2db` | `PRODUCT_WLHUASHU` 样例，10 行，含拒绝原因维度 |
| `F0963320261120000000.AVL` | `sftp_avl2db` | `CONF_USER_SCENE` 样例，10 行，含痛点诊断文本；该 AVL 按 `product_no` 分 128 张表，样例行会分散到不同 `F09633_{i}` |
| `F0963520261120000000.AVL` | `sftp_avl2db` | `TAGID_TAGNAME` 样例，10 行（表主键 = `tag_name`，供诊断按标签名反查 `tag_id`） |
| `iop_labels_query_raw_response.json` | `IOP` | 标签查询接口的原始响应样例：`res_code` / `res_desc` / `result.BUSIINFO[]`，每项含 `ITEMCODE` / `ITEMNAME` / `ITEMVALUE` |
| `卖点维表.xlsx` | `local_excel2db` | `selling_points` 表的格式样例（业务 ID 表头 → 库列 `product_id`、话术、标签 ID、`标签0…标签27` 等） |
| `标签转换维表.xlsx` | `local_excel2db` | `label_name_mapping` 表的格式样例，取名为「标签名称--标签ID」的 sheet |

AVL 命名遵循 `{业务前缀}{YYYYMMDD}000000.AVL`，制表符分隔、UTF-8、无表头。字段含义、表结构 / 主键 / 查询键与 AVL 映射见 [`sftp_avl2db/README.md`](../sftp_avl2db/README.md) 与 `sftp_avl2db/avl_specs.py`、`sftp_avl2db/table_schemas.py`；诊断链路的完整取数规则见 [`services/README.md`](../services/README.md)。

## 2. 三个子系统分别怎么用这些样例

**SFTP / AVL 离线复跑**（只吃 `*.AVL`）

```bash
python -m sftp_avl2db --task F09591 --date 20261110 \
  --local-dir io_samples --runtime-dir /tmp/replay
```

`--local-dir io_samples` 让拉取任务把本目录当成本地 AVL 源，`--runtime-dir` 把产物隔离到临时目录，不碰包内 `sftp_avl2db/runtime/`。生产环境里 AVL 落在 `sftp_avl2db/runtime/files/{项目名}/`，输出的 `{AVL}.db` 落在 `sftp_avl2db/runtime/sqlite_db/{项目名}/`。

注意 `--local-dir` 只替换远程源：命令仍会装配 `TaskLogger`，需先装 `loguru`，未装时退出码 2，报「TaskLogger 默认使用 loguru；请安装 loguru，或注入 logger=InMemoryLogger() / 宿主 logger 对象」（详见 [`sftp_avl2db/README.md` 第 8 节](../sftp_avl2db/README.md)）。

**IOP 响应对照**（只吃那个 `.json`）

`iop_labels_query_raw_response.json` 是排查字段口径时看的原始报文形状；在线链路里 `IOPClient` 会把它解析成 `{ITEMCODE: [名称, 值]}` 的画像 dict，不做打桩、不读这个文件。想验证真实上游用 [`tests/check_iop_direct.py`](../tests/README.md)。

`F09630` / `F09631` / `F09632` 的话术样例里含 `#客运个性化运营标签34&`、`#客运个性化运营标签82&`、`#互联网融合标签94&` 这类占位符：在线诊断链路按标签名查 `F09635` 拿 `tag_id`（= `ITEMCODE`），再用 IOP 返回的 `ITEMVALUE` 替换；平级的卖点话术模块不做这类替换。样例里的标签名与 `F09635` / JSON 样例均为脱敏数据、不相互对应，仅供格式对照。

**维表 Excel**（只吃两个 `.xlsx`，且见下节）

`local_excel2db` 按包内 `registry.json` 的 `source`（纯文件名）从项目根 `excels/` 轮询 xlsx、抽列写入 `local_excel2db/runtime/sqlite_db/dimensions.db`；在线侧经 `services/dimensions.py` 每请求只读一次该库，按 `product_id` 取 `selling_points.label_id`（Excel「标签ID」）拼商机辅助画像；诊断 8 表不适用时，由平级的卖点话术模块把该行的产品名称 /「生成话术」/「兜底话术」/「匹配结果2」/「标签N」原文转换成上一版 `ProductInfoRewrite` 的输出格式。

## 3. 维表 Excel 的实际目录：`excels/`

| 目录 | 角色 | 谁读它 |
|---|---|---|
| `io_samples/` | 随仓库提交的格式样例，用于对照表头 / 列格式 | 无人 |
| `excels/` | `local_excel2db` 唯一输入目录，按 `__file__` 定位到项目根 | `python -m local_excel2db` 写入进程 |
| `local_excel2db/runtime/sqlite_db/` | 写入侧唯一产物 `dimensions.db` | `services/dimensions.py`（在线侧 `mode=ro`，每请求 1 次） |

`registry.json` 里每张表的 `source` 只允许纯文件名（如 `卖点维表.xlsx`），由 `config.py` 强制拼到 `INPUT_DIR = 项目根/excels/`；写 `../`、子目录或绝对路径会在加载配置时直接报 `ValueError`。库、锁、日志的路径是代码常量，全部落在 `local_excel2db/runtime/` 下；配置与产物都锚在包内，与启动 CWD 无关，registry 与 CLI 都不提供路径覆盖入口，也不读环境变量：

```bash
python -m local_excel2db --once
```

仓库已把本目录的两张 xlsx 原样预置在 `excels/` 里（内容完全一致），它本身就是 `local_excel2db` 的默认输入，直接跑 `python -m local_excel2db --once` 即可验证，不需要再复制覆盖；只有想换成自制样例时才覆盖 `excels/` 下的同名文件。更新维表直接改 `excels/` 里的文件并重新提交，不要写进 `.gitignore`。改表名、列映射或主键后要删库重建：`rm local_excel2db/runtime/sqlite_db/dimensions.db`。配置说明见 [`local_excel2db/README.md`](../local_excel2db/README.md)。

## 4. 数据与脱敏

AVL 样例里含手机号、通话总结、拒绝原因等话术文本，JSON 样例里含 `ITEMVALUE`，两张 xlsx 含产品名与话术——都是脱敏后的样例数据，不代表真实业务内容。往这里补新样例前先确认无真实客户信息；生产数据一律走 `sftp_avl2db/runtime/`、`local_excel2db/runtime/`、`excels/` 等运行时目录，项目 `.gitignore` 已忽略 `*.db`、`*.log`、各包 `runtime/` 等产物。

`tests/` 里的手工冒烟脚本打的是运行中的服务，不使用这些样例文件。
