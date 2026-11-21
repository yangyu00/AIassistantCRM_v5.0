# tests/ · 手工冒烟脚本

这里放的是**部署后/联调时手动跑的请求脚本**，不是 pytest 单元测试：

- 没有 `test_` 开头的函数，没有 `assert`，没有 fixture，文件名也**故意不叫 `test_*.py`**，避免被 pytest 收集；
- 每个脚本跑一次、发一次真实请求、把响应原样打印出来，结论靠人看；
- 不打桩、不 mock，会真实访问在线服务或 IOP 上游。

本目录收录主链路、入参校验、调用量统计、IOP 直连与诊断离线自检脚本。

## 前置条件

1. 起在线服务：`python main.py`（默认 `0.0.0.0:8989`，见 `main.py` 顶部 `FASTAPI_CONFIG`）。
2. 三个写入侧进程按需起；没起也能跑，只是业务库/维表未就绪时话术走降级分支（`code: "2"`）。
3. `check_iop_direct.py` 还需要 `IOP/.env` 配好凭据、并装好 `IOPClient` 的依赖（aiohttp 等）。

**地址**：默认 `http://127.0.0.1:8989`。服务在别的机器上不用改代码，设环境变量即可：

```bash
export CRM_SMOKE_BASE_URL=http://10.0.0.5:8989
```

业务路由 `/api/speechQryService` 与统计路由 `/api/callStat` 分别对应 `main.py` 的
`MARKET_ASSISTANT_URL`、`call_stat/.env` 的 `CALL_STAT_QUERY_PATH`；改了这两处，脚本顶部的
同名常量要跟着改。

## 脚本清单

| 脚本 | 打谁 | 看什么 |
|---|---|---|
| `check_online_api.py` | `POST /api/speechQryService` | 主链路：耗时、完整响应、逐条话术的 `code` 与长度。可传号码和多个产品 ID |
| `check_param_validation.py` | `POST /api/speechQryService` | 手机号 / 产品 ID 的非法入参，期望 `res_code=1`；附一组正常请求做对照 |
| `check_call_stat.py` | `GET /api/callStat` | 单日、整月、连续两次查询（计数规则）、4 组非法参数 |
| `check_iop_direct.py` | IOP 上游（**不经在线服务**） | 单独查标签画像，用来区分「上游挂了」还是「在线编排有问题」 |
| `check_diagnosis_offline.py` | **不打任何服务**（临时 SQLite + 内存桩） | 诊断新线端到端自检：注册 / 解析 / 入库 → 多行查询 → 7 表关联 → 话术拼装 → `main.py` 路由，结论 `ALL PASS` |

## 运行

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

# 诊断新线离线自检：不需要起服务、不连 SFTP / IOP，临时库跑完即删
python tests/check_diagnosis_offline.py
```

## 副作用与注意

- **会改调用量**：凡是经过在线服务的请求（包括 `GET /api/callStat` 自己）都会被
  `count_call_middleware` 计数，跑完当天 `callCount` 会涨。这是正常行为，不是测试污染。
- **GET 查询不含本次**：计数在 `finally` 阶段，所以某次查询返回的 `callCount` 不含它自己；
  连续两次查询时，第二次会把第一次算进去。
- `check_iop_direct.py` 会真实刷新 token、消耗上游调用，**不经过在线服务也不计调用量**。
- 脚本只打印，不写文件；服务侧日志照常落在项目根 `log/`。
- **例外**：`check_diagnosis_offline.py` 不打服务，在临时目录建 8 个诊断库跑完即删；
  它会打桩 `IOPClient.get_customer_labels`（不消耗上游 token），并按 `.env.example`
  临时补一份 `sftp_avl2db/.env` 供配置加载，结束后删除。末尾的在线路由段需要
  fastapi / pydantic / loguru / aiohttp，缺依赖会自动跳过，不影响主体断言。
- 这里的示例号码与产品 ID 都是占位值，换成真实数据才能拿到业务话术。

## 为什么没有 pytest

这些脚本依赖一个已经起来的服务、真实上游和真实库，属于「部署后自检 / 联调」，
放进 CI 当单元测试会互相干扰（计数、token、上游额度）。需要可重复的纯逻辑断言时，
用 `python -m call_stat --self-check`（组件自带、临时库用完即删），两者定位不同。
