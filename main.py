from contextlib import asynccontextmanager
from typing import List

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator

from IOP import IOPClient
from call_stat import startup, flush, count_call_middleware, build_router
from services import (
    BUSINESS_OBJECT_TYPES, build_product_object, dimensions, need_labels,
)
from src.exceptions import telnumError, productIdError, register_errors_handlers
from src.log_util import my_log, configure_logging
from app_config import resolve_port

# 日志目录：留空表示写到项目根目录下的 log/，也可填绝对路径
LOG_DIR = ""

# 在线服务监听端口：唯一来源是项目根 .env 的 CRM_PORT（默认 8990），
# 由 app_config.load_config() 解析；改端口只改 .env 一行并重启进程。
FASTAPI_CONFIG = {"host": "0.0.0.0", "port": resolve_port(), "reload": False}
# 业务路由
MARKET_ASSISTANT_URL = "/api/speechQryService"

# 业务路由（product_id → AVL 前缀、是否查 IOP 标签、产品对象工厂）统一在
# services/routing.py，由 services/__init__.py 导出；本文件只保留在线服务的
# 运行参数与请求级编排。分层与「四类业务 + 兜底」路由见 services/README.md。

configure_logging(LOG_DIR)

# 进程内共享一个客户端：token 状态与 HTTP 会话由它自持，标签结果仍按请求隔离。
# 必须注入在线进程 logger：IOP 默认 NullLogger 会把刷新失败 / 冷却降级 /
# 标签查询失败全部静默吞掉，线上 log/ 里查不到「为什么这次没有辅助画像」。
iop_client = IOPClient(logger=my_log)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时恢复调用量基数并创建 IOP 会话，关闭时收尾。"""
    base = startup()
    my_log.info(f"调用量统计已恢复：当天已累计 {base} 次（重启后继续计数）")
    await iop_client.start()
    try:
        yield
    finally:
        await iop_client.aclose()
        flush()


# 后台写入侧（sftp_avl2db / local_excel2db）各自独立进程把数据写进磁盘；
# 本进程只读磁盘上的 db 文件：读哪些库、库里有哪些列、怎么查，全部由
# services/db_config.py 独立声明，不引用后台写入侧的任何代码或配置。
# 每请求经 services.dimensions 只读 dimensions.db：批量查 selling_points
# （商机标签 ID 子集、卖点话术模块的产品名称 /「生成话术」/「兜底话术」/
# 「匹配结果2」/「标签N」原文），再全表读一次 label_name_mapping（标签显示名）。
# 两次都是 mode=ro、无进程级缓存；经 services.reader
# 只读业务库；唯一写入是调用量统计（call_stat/runtime/call_sum.db）。
app = FastAPI(lifespan=lifespan)
register_errors_handlers(app)
app.middleware("http")(count_call_middleware)
app.include_router(build_router())


class ProductIds(BaseModel):
    id: str = Field("", description="产品ID", example="******")

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str):
        if not value:
            raise productIdError(value)
        return value


class Args(BaseModel):
    sceneId: str = Field("", description="场景ID", example="******")
    telnum: str = Field("", description="手机号", example="15020868271")
    productId: List[ProductIds] = Field([], description="产品ID列表",
                                        example=[{"id": '******'},
                                                 {"id": '******'}])

    @field_validator("telnum")
    @classmethod
    def validate_telnum(cls, value: str):
        if not value or not value.isdigit() or len(value) != 11:
            raise telnumError(value)
        return value

    @field_validator("productId")
    @classmethod
    def validate_productId(cls, value: List[ProductIds]):
        if not value:
            raise productIdError(value)
        return value


class IopLabelProvider:
    """一次请求内的标签惰性提供者，负责同请求只查一次并隔离不同请求。"""

    def __init__(self, telnum):
        self.telnum = telnum
        self._labels = None
        self._fetched = False
        # 本次请求 IOP 的降级原因（"" 表示未发起查询，见 need_labels）。
        self.degraded_reason = ""

    async def labels(self):
        if self._fetched:
            return self._labels

        try:
            self._labels, self.degraded_reason = (
                await iop_client.get_customer_labels_with_status(self.telnum))
            self._fetched = True
            if not self._labels:
                # 空标签 = 无【辅助画像】且切入/挽留话术回落兜底组。
                # 以前这里完全没有日志，只能从话术结果反推 IOP 是否成功。
                # 区分「上游成功但号码无标签」与「IOP 降级」：前者 INFO、后者 ERROR。
                normal = self.degraded_reason in ("ok", "ok_empty")
                log_fn = my_log.info if normal else my_log.error
                log_fn(
                    f"IOP 标签为空 | telnum_len={len(self.telnum)} | "
                    f"reason={self.degraded_reason} | kind="
                    f"{'号码无标签（上游正常）' if normal else 'IOP 降级'} | "
                    f"影响：本次请求无辅助画像，切入/挽留回落兜底组")
        except Exception as e:
            my_log.error(e)
            self._labels = {}
            self.degraded_reason = "provider_exception"
            self._fetched = True
        return self._labels


@app.post(MARKET_ASSISTANT_URL)
async def marketAssistant(args: Args):
    # 标签提供者按请求创建：标签属于单个手机号，不能放在模块级或 app.state
    # 缓存，否则会把上一个用户的标签带给下一个用户且不会报错
    label_provider = IopLabelProvider(args.telnum)

    # 建立产品对象列表
    # 卖点维表按本次请求的全部 product_id 批量查一次（不缓存、不跨请求共享）：
    # 商机对象的标签 ID 子集来自该行的「标签ID」列，卖点话术模块用该行的产品
    # 名称 /「生成话术」/「兜底话术」/「匹配结果2」/「标签N」原文
    # 拼话术；库缺失 / 未命中降级为空 dict。
    selling_points = dimensions.load_selling_points(
        [one.id for one in args.productId])
    # 「标签转换维表.xlsx」写出的 label_name_mapping 表：把 IOP 的 ITEMNAME
    # 换成营销助手显示名字。
    # 表缺失 / 列漂移降级 {}，辅助画像直接用 IOP 原名，输出结构不变。
    label_name_mapping = dimensions.load_label_name_mapping()
    product_obj_list = []
    for one_product_obj in args.productId:
        # 商机回捞（辅助画像）、诊断链路（话术 #标签名& → IOP 值）与卖点话术
        # 模块（辅助画像 + 命中哪组切入 / 挽留话术）都需要标签，由业务层
        # need_labels() 声明；提供者保证整请求最多查一次 IOP，控损不触发查询。
        # 逐个判断等价于对全部 ID 取并集，不会因首个 ID 不需要而漏查。
        # 「要不要拼画像」不在这里判断：商机对象持有画像组件，卖点话术按维表
        # 「标签ID」子集自行拼装，空标签 / 空标签 ID 子集时自然返回空串。
        custom_label_info = (await label_provider.labels()
                             if need_labels(one_product_obj.id) else {})
        product_obj_list.append(await build_product_object(
            one_product_obj.id, args.telnum,
            custom_label_info=custom_label_info,
            selling_points=selling_points,
            label_name_mapping=label_name_mapping))

    if (label_provider.degraded_reason
            and label_provider.degraded_reason not in ("ok", "ok_empty")):
        my_log.error(
            f"本次请求 IOP 降级 | reason={label_provider.degraded_reason} | "
            f"product_count={len(args.productId)} | "
            f"影响：无辅助画像 + 切入/挽留话术回落兜底组")

    # 输出话术
    # 商机回捞 / 控损 / 诊断 / 卖点话术四类业务对象返回 code=0；其余兜底 code=2。
    # 这里按对象类型判定，不能靠 product_name 是否为空：兜底对象也可能因
    # 维表残留拿到非空名称，只看它会返回 code=0 + 空话术，看起来成功、实际无内容。
    allScripts = []
    for product_obj in product_obj_list:
        my_log.info(product_obj)
        if isinstance(product_obj, BUSINESS_OBJECT_TYPES):
            allScripts.append(
                {"productId": product_obj.product_id, "productName": product_obj.product_name,
                 "script": product_obj.output_script, "code": "0"})
        else:
            my_log.error("无产品名称")
            my_log.error(product_obj)
            allScripts.append(
                {"productId": product_obj.product_id, "productName": "", "script": "", "code": "2"})

    result = {"allScripts": allScripts}
    return {"res_code": 0, "res_desc": "", "result": result}


if __name__ == "__main__":
    uvicorn.run(app="main:app", **FASTAPI_CONFIG)
