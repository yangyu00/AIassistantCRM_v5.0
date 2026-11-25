# tests/ · 手工冒烟脚本

部署后 / 联调时手动跑的冒烟脚本，不是 pytest 单元测试：没有 `test_` 开头的函数、没有 fixture，文件名也故意不叫 `test_*.py`，避免被 pytest 收集。按是否联网分两类：

- **在线冒烟**：`check_online_api.py` / `check_param_validation.py` / `check_call_stat.py` / `check_iop_direct.py` —— 跑一次、发真实请求、打印响应，不打桩、不 mock，会真实访问在线服务或 IOP 上游，结论靠人看；
- **离线自检**：`check_diagnosis_offline.py` / `check_legacy_output_parity.py` / `check_purge_rotation.py` / `check_avl_freshness.py` —— 不打任何服务，用临时 SQLite + 内存桩 / 桩时钟跑断言，打印 `ALL PASS`，临时产物跑完即删。`legacy_output_reference.py` 是上一版本输出的独立参照实现（oracle），本身不跑断言、不 import 现版模块。

## 1. 前置条件

1. 起在线服务：`python main.py`（默认 `0.0.0.0:8990`，端口由项目根 `.env` 的 `CRM_PORT` 配置）。
2. 写入侧进程按需起；没起也能跑：业务库未就绪时商机 / 控损按空记录出话术（均 `code: "0"`）、诊断返回 `None` 后由平级的卖点话术模块给出四段话术（`code: "0"`），维表里也没有该 `product_id` 才落 `code: "2"`；卖点维表未就绪时商机辅助画像为空、卖点话术无行可用。`code` 始终按路由对象类型判定，不抛 500。
3. `check_iop_direct.py` 还需要 `IOP/.env` 配好凭据、并装好 `IOPClient` 的依赖（aiohttp 等）。

地址默认 `http://127.0.0.1:8990`。服务在别的机器上、或改了项目根 `.env` 的 `CRM_PORT`，都不用改代码，设环境变量即可：

```bash
export CRM_SMOKE_BASE_URL=http://10.0.0.5:8990
```

业务路由 `/api/speechQryService` 与统计路由 `/api/callStat` 分别对应 `main.py` 的 `MARKET_ASSISTANT_URL`、`call_stat/.env` 的 `CALL_STAT_QUERY_PATH`；改了这两处，脚本顶部的 `SPEECH_PATH` / `CALL_STAT_PATH` 常量要跟着改。

## 2. 脚本清单

| 脚本 | 打谁 | 看什么 |
|---|---|---|
| `check_online_api.py` | `POST /api/speechQryService` | 主链路：耗时、完整响应、逐条话术的 `code` 与长度。可传号码和多个产品 ID |
| `check_param_validation.py` | `POST /api/speechQryService` | 手机号 / 产品 ID 的非法入参，期望 `res_code=1`；附一组正常请求做对照 |
| `check_call_stat.py` | `GET /api/callStat` | 单日、整月、连续两次查询（计数规则）、4 组非法参数 |
| `check_iop_direct.py` | IOP 上游（不经在线服务） | 单独查标签画像，用来区分「上游挂了」还是「在线编排有问题」 |
| `check_diagnosis_offline.py` | 不打任何服务（临时 SQLite + 内存桩） | 诊断端到端自检（**卖点话术部分把上一版 `ProductInfoRewrite.output_script` 固化成 `EXPECTED_LEGACY_OUTPUT`，离线与在线两处逐字符断言相等；另用 `tests/legacy_output_reference.py` 对 IOP 辅助画像 + 显示名映射 + 标签专属话术做 golden 逐字符比对**）：注册 / 解析 / 入库 → `F09633` 分表（128 张、号码不串表、按 telnum 定位）→ 卖点维表 → 商机标签 ID / 辅助画像 → 业务包命名与商机 / 控损话术输出 → 业务库未就绪时按空记录出话术 → 平级卖点话术模块（切入 / 卖点 / 挽留的蓝 / 红 span 与加粗标题、段间 `\n`、老版取数口径、按标签选话术可选路径、无该行 / 产品名为空降级、`#标签名&` 原样保留）→ 诊断不适用（db 查空 / 交集空 / 六项全空 → `None`）→ `services.reader` 多行查询 → 7 表关联 → 六段话术拼装 → 五类路由（含卖点话术 `code=0`）；商机 / 诊断共用请求级 IOP 标签、整请求 1 次；`F09635` 主键 `tag_name` 与诊断三类话术 `#标签名&` → `ITEMVALUE` 替换，结论 `ALL PASS` |
| `check_legacy_output_parity.py` | 只读真实 `dimensions.db`（`mode=ro`） | 卖点话术**全量逐字符复现**自检：用 `legacy_output_reference.py` 作独立 oracle，对库里全部 `product_id`（当前 1361 个）分别构造「无标签 / 命中首个 / 末尾 / 前三个场景 / 画像标签」等 IOP 返回，比对 `output_script` 与 `productName`；当前 5121 次输出、含辅助画像 896 次、不一致 0，结论 `ALL PASS`。维表库不存在时打印 SKIP 并返回 0 |
| `legacy_output_reference.py` | 不打任何服务（纯函数） | 上一版四个历史文件的逐行直译，提供 `build_legacy_consts()` / `legacy_output()`，供上面两个自检脚本当 oracle；**不参与线上运行**，也不 import 现版实现 |
| `check_purge_rotation.py` | 不打任何服务（临时 SQLite + 桩时钟） | 库表清理轮转自检：AVL 粒度配置（缺键 fail-fast / 逐 AVL 独立取值）/ 7 组并集覆盖 128 张表且组间互斥 / `cycle=0` 行为 / 固定表每日全清 / 每天只触发一次 `purge_expired()` / 跨 7 天过期行全清而新鲜行保留 / 单次补跑一次全量清，结论 `ALL PASS` |
| `check_avl_freshness.py` | 不打任何服务（临时 SQLite） | AVL 批次新鲜度窗口自检：`_avl_meta` 水位登记 / 补跑旧文件不回退 / 窗口下界 / 单条倒序取最新 / 多条收敛到最新批次 / `N=0` 与元数据表缺失降级，结论 `ALL PASS` |
| `check_drop_before_insert.py` | 不打任何服务（临时 SQLite） | 配置变更自检：诊断 8 张表 `DATA_EXPIRE_DAYS=365` / 商机控损仍为 3 / 按业务新鲜度窗口（商机 2 · 控损 2 · 诊断 0）/ 旧全局键已废弃写了不生效 / 离线单值注入 / `AVL_DROP_BEFORE_INSERT` 全局开关与 `<AVL>_` 覆盖 / 模式关闭按主键追加、开启时 DROP 重建固定表与 128 张分表且 `_avl_meta` 保留，结论 `ALL PASS` |

## 3. 运行

```bash
# 主链路：默认示例值，或自己给号码和多个产品 ID
python tests/check_online_api.py
python tests/check_online_api.py 18866816199 202502120000001
python tests/check_online_api.py 18866816199 ID1 ID2 ID3

# 入参校验：连打 9 组（含 1 组正常对照），打印每组的期望与实际
python tests/check_param_validation.py

# 调用量统计：查今天与本月，可指定日期
python tests/check_call_stat.py
python tests/check_call_stat.py 2026-11-16

# IOP 直连（必须给号码，或填脚本里的 DEFAULT_TEL）
python tests/check_iop_direct.py 13900000000

# 想看统计涨没涨，按这个顺序跑
python tests/check_call_stat.py 2026-11-16
python tests/check_online_api.py
python tests/check_call_stat.py 2026-11-16

# 诊断离线自检：不连 SFTP / IOP，临时库跑完即删
python tests/check_diagnosis_offline.py

# 卖点话术全量逐字符对照：需先生成维表库
python -m local_excel2db --once
python tests/check_legacy_output_parity.py

# 库表清理轮转自检：不写包内 runtime/，临时库跑完即删
python tests/check_purge_rotation.py

# AVL 批次新鲜度窗口自检：不写包内 runtime/，临时库跑完即删
python tests/check_avl_freshness.py

# 配置变更自检：不写包内 runtime/，临时库跑完即删
python tests/check_drop_before_insert.py
```

## 4. 副作用与注意

- 凡是经过在线服务的请求（包括 `GET /api/callStat` 自己）都会被 `count_call_middleware` 计数，跑完当天 `callCount` 会涨。
- GET 查询计数在 `finally` 阶段，某次查询返回的 `callCount` 不含它自己；连续两次查询时第二次会把第一次算进去。
- `check_iop_direct.py` 会真实刷新 token、消耗上游调用，不经过在线服务也不计调用量。
- 在线冒烟脚本只打印，不写文件；服务侧日志照常落在项目根 `log/`。
- `check_legacy_output_parity.py` 只读真实 `dimensions.db`（`mode=ro`），不写库、不连 IOP；库不存在时 SKIP。它依赖 `label_name_mapping` 表提供显示名映射，缺表时该映射为空、oracle 与现版同时降级，比对仍成立。
- `check_diagnosis_offline.py` 会在临时目录建 8 个诊断库跑完即删，并打桩 `IOPClient.get_customer_labels`（不消耗上游 token）。写入侧 `load_profiles()` 读包内 `sftp_avl2db/.env`；干净检出里缺失时，脚本会从 `.env.example` 临时复制一份（含 `F09633_NUM_TABLES` 分表数）并在结束后删除。末尾的在线路由段需要 fastapi / pydantic / loguru / aiohttp / uvicorn，缺任一依赖会自动跳过，不影响主体断言。
- `check_purge_rotation.py` 只用标准库 + 临时 SQLite，跑完即删；它用 `sftp_avl2db/.env` 验证 `load_profiles()` 装配，干净检出里缺失时会从 `.env.example` 临时复制、结束后删除。
- 示例号码与产品 ID 都是占位值，换成真实数据才能拿到业务话术。

## 5. 为什么没有 pytest

这些脚本依赖一个已经起来的服务、真实上游和真实库，属于「部署后自检 / 联调」，放进 CI 当单元测试会互相干扰（计数、token、上游额度）。需要可重复的纯逻辑断言时，用 `python -m call_stat --self-check`（组件自带、临时库用完即删），两者定位不同。
