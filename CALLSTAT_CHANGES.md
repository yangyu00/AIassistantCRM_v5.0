# 本次改动与部署说明

> 本文件说明「调用量统计拆成独立模块」这次改动；组件自身完整文档见 [`call_stat/Readme.md`](call_stat/Readme.md)，全局视角见 [`README.md`](README.md)。

## 改了什么

| 位置 | 改动 |
|---|---|
| 新增 `call_stat/` | 独立模块：`.env` / `.env.example`、`Readme.md`、`__init__.py`、`__main__.py`、`config.py`、`store.py`、`counter.py`、`query.py`、`web.py`、`cli.py`、`runtime/.gitkeep` |
| `main.py` | 改为 `from call_stat import startup, flush, count_call_middleware, build_router`；模块级 `app.middleware(...)` + `app.include_router(build_router())`；lifespan 内 `startup()` / `flush()`；删除内联 middleware、两个日期校验函数与 GET 路由；更新注释里的产物路径 |
| `config.yaml` | 删除 `call_stat:` 段（阈值与查询路径改由 `call_stat/.env` 提供） |
| `.gitignore` | 增加 `call_stat/runtime/*` 忽略与 `.gitkeep` / 无凭据 `.env` 例外 |
| `src/call_stat.py` | 删除（原逻辑全部平移进 `call_stat/`） |
| `README.md` | 文档地图加第四个组件；降级总表改路径并指向模块 Readme；原「调用量统计」整章替换为「调用量统计接入」；自检要点改为指向 `python -m call_stat --self-check` |

**统计语义零改动**：表结构、`UPDATE ... SET call_count = call_count + ?` 累加、阈值缓冲、跨天残留重试、写失败保留 `_PENDING`、重启从库恢复基数、GET 两参数互斥与 `res_code` 响应形状、lifespan + `atexit` 双幂等，全部保持原样。

配置与产物边界：唯一来源 `call_stat/.env`，唯一产物目录 `call_stat/runtime/`（按 `__file__` 定位），代码里拒绝 `../` 与绝对路径，产物不可能越出模块目录。

## 已验证

- 全仓 `.py` 编译通过；残留引用检查无 `from src import call_stat` / `src.call_stat` / 根配置 `call_stat` 段。
- 真实 FastAPI（fastapi 0.162 / starlette 1.7 / pydantic 2.14）+ TestClient：`/api/callStat` 与 `/api/speechQryService` 均注册；`app.router.on_startup` / `on_shutdown` 为空、走 lifespan；middleware 每次请求计数；GET 单日 / 整月（补满 30 天）/ 非法参数响应正确；shutdown `flush()` 后库里为 5；清空内存模拟冷启动恢复基数仍为 5；业务 POST 在维表与业务库缺失时仍返回 200 + `code: "2"` 降级。
- `python -m call_stat --self-check` 通过（阈值落库 / 重启恢复 / 月补齐 / 参数校验 / 产物边界）；`--date` / `--month --export` 输出落在 `call_stat/runtime/export/`。
- 打包前已清理全部运行时产物（库、CSV、日志、`__pycache__`），`call_stat/runtime/` 只保留 `.gitkeep`。

## 部署注意

1. **`sftp_avl2db/.env` 是从 `.env.example` 复制的模板**，`SFTP_PASSWORD` 仍是占位值 `<填入真实密码>`；它为的是让项目可开箱 import / 启动，部署前必须填真实密码。`IOP/.env` 是原仓库自带的明文凭据文件，按 `.gitignore` 不应入库，分发前请自行处理。
2. **已有旧库时做一次停机迁移**：
   ```bash
   python -m call_stat --migrate-from log/call_sum.db
   ```
   目标 `call_stat/runtime/call_sum.db` 已有数据会拒绝执行，避免重复累加；只挪库文件，**不要删 `log/` 目录**（`src/log_util.py` 仍在那里写 INFO/ERROR 日志）。迁移后重启在线进程，`startup()` 读回基数，数字连续。
3. 进程数量不变，仍是 4 个（2 个 SFTP 拉取 + 1 个维表写入 + 1 个在线 API）；`call_stat` 是在线进程内模块，不需要额外常驻进程。`python -m call_stat ...` 是按需查询/维护命令。
4. 调整阈值或查询路径只改 `call_stat/.env` 的 `CALL_STAT_FLUSH_THRESHOLD` / `CALL_STAT_QUERY_PATH`，**重启在线进程**生效；根 `config.yaml` 不再有相关配置。
5. 部署时不要整目录覆盖 `call_stat/`，需要把数据挂到独立盘时把 `call_stat/runtime/`（以及 `sftp_avl2db/runtime/`）做成软链。

## 常用命令

```bash
python -m call_stat --self-check
python -m call_stat --date 2026-11-16
python -m call_stat --month 2026-11 --export
python -m call_stat --flush
curl "http://host:8989/api/callStat?date=2026-11-16"
curl "http://host:8989/api/callStat?month=2026-11"
```
