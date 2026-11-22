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

# 日志目录：留空表示写到项目根目录下的 log/，也可填绝对路径
LOG_DIR = ""
FASTAPI_CONFIG = {"host": "0.0.0.0", "port": 8989, "reload": False}
# 业务路由；users 为预留项，当前业务代码未读取
MARKET_ASSISTANT_URL = "/api/speechQryService"
MARKET_ASSISTANT_USERS = ["CRM"]

# 业务路由（product_id → AVL 前缀、是否查 IOP 标签、产品对象工厂）统一在
# services/routing.py，由 services/__init__.py 导出；本文件只保留在线服务的
# 运行参数与请求级编排。分层与四类路由见 services/README.md。

configure_logging(LOG_DIR)

# 进程内共享一个客户端：token 状态与 HTTP 会话由它自持，标签结果仍按请求隔离。
iop_client = IOPClient()


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
# 每请求经 services.dimensions 批量只读一次 dimensions.db（商机标签 ID 子集与
# 诊断兜底所需的产品名称 /「兜底话术」/「标签N」原文），经 services.reader
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

    async def labels(self):
        if self._fetched:
            return self._labels

        try:
            self._labels = await iop_client.get_customer_labels(self.telnum)
            self._fetched = True
        except Exception as e:
            my_log.error(e)
            self._labels = {}
            self._fetched = True
        return self._labels


@app.post(MARKET_ASSISTANT_URL)
async def marketAssistant(args: Args):
    # 标签提供者按请求创建：标签属于单个手机号，不能放在模块级或 app.state
    # 缓存，否则会把上一个用户的标签带给下一个用户且不会报错
    label_provider = IopLabelProvider(args.telnum)

    # 2. 建立产品对象列表
    # 卖点维表按本次请求的全部 product_id 批量查一次（不缓存、不跨请求共享）：
    # 商机对象的标签 ID 子集来自该行的「标签ID」列，库缺失/未命中降级为空 dict。
    selling_points = dimensions.load_selling_points(
        [one.id for one in args.productId])
    product_obj_list = []
    for one_product_obj in args.productId:
        # 只有商机回捞查标签，由业务层 need_labels() 声明；提供者保证整请求
        # 最多查一次 IOP，诊断 / 控损 / 兜底都不触发查询。
        # 逐个判断等价于对全部 ID 取并集，不会因首个 ID 不需要而漏查。
        # 「要不要拼画像」不在这里判断：只有商机对象持有画像组件，空标签 / 空标签 ID 子集时它自然返回空串。
        custom_label_info = (await label_provider.labels()
                             if need_labels(one_product_obj.id) else {})
        # 四类路由（商机 / 控损 / 诊断 / 兜底）、取数与对象构造都在
        # services/routing.py::build_product_object；本函数只做请求级编排
        product_obj_list.append(await build_product_object(
            one_product_obj.id, args.telnum,
            custom_label_info=custom_label_info,
            selling_points=selling_points))

    # 3. 输出话术
    # 商机回捞 / 控损 / 诊断三类业务对象返回 code=0；其余兜底 code=2。
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
