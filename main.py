from contextlib import asynccontextmanager
from enum import Enum
from typing import List

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator

from IOP import IOPClient
from call_stat import startup, flush, count_call_middleware, build_router
from services.diagnosis import ProductInfoByDiagnosis, build_diagnosis_record
from services.product_info_raw import ProductInfoRaw as ProductInfoRaw
from services.rewrite_by_spec_f09554.rewrite import ProductInfoRewrite as ProductInfoBySpec9554
from services.rewrite_by_spec_f09591.rewrite import ProductInfoRewrite as ProductInfoBySpec9591
from sftp_avl2db import data_query
from src.exceptions import telnumError, productIdError, register_errors_handlers
from src.log_util import my_log, configure_logging

# ===== 在线服务配置（端口、路由、日志目录） =====
# 日志目录：留空表示写到项目根目录下的 log/，也可填绝对路径
LOG_DIR = ""
# uvicorn 启动参数，对应原 config.yaml 的 fastapi 段
FASTAPI_CONFIG = {"host": "0.0.0.0", "port": 8989, "reload": False}
# 业务路由；users 为预留项，当前业务代码未读取
MARKET_ASSISTANT_URL = "/api/speechQryService"
MARKET_ASSISTANT_USERS = ["CRM"]

# ===== 业务路由：product_id → AVL 前缀（本文件是唯一维护点）=====
# 数据访问层 sftp_avl2db 只认 AVL 前缀；产品 ID 属业务概念，不进查询层。
# 商机回捞 / 控损是静态映射：新增产品 ID 只改下面两张表，把 AVL 前缀传给
# data_query.query(avl, telnum)。
# 诊断新线不同：F09629（PRODUCT_BUSINESS）本身就是「产品ID → 策略/产品」的
# 映射表，不需要在 main.py 再维护一张静态表；命中该表即走诊断业务，
# 由 services/diagnosis 按 AVL 前缀关联其余 6 张维表，读取侧仍只认 AVL 前缀。
OPPORTUNITY_RECALL_AVLS = {
    "202608041615335173": "F09554",  # 号卡业务
    "202608041615335172": "F09555",  # 套餐业务
    "202608041615335174": "F09556",  # 宽带业务
}
LOSS_CONTROL_AVLS = {
    "202609180955095402": "F09591",  # 控损
}
OPPORTUNITY_RECALL_PRODUCT_IDS = frozenset(OPPORTUNITY_RECALL_AVLS)
LOSS_CONTROL_PRODUCT_IDS = frozenset(LOSS_CONTROL_AVLS)
# ==========================================================================================

configure_logging(LOG_DIR)

# 进程内共享一个客户端：token 状态与 HTTP 会话由它自持，标签结果仍按请求隔离。
iop_client = IOPClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时恢复调用量基数并创建 IOP 会话，关闭时收尾。

    IOP 会话必须在同一个 event loop 内创建/关闭，故挂在 lifespan 而非 atexit
    （atexit 拿不到运行中的 loop）。uvicorn 的 SIGTERM 退出路径不经过 atexit，
    由 yield 之后显式收尾；call_stat 仍注册 atexit 作为非 ASGI 运行方式的
    兜底，两处幂等。startup 里不做任何定时任务，只做一次性初始化。
    """
    base = startup()
    my_log.info(f"调用量统计已恢复：当天已累计 {base} 次（重启后继续计数）")
    await iop_client.start()
    try:
        yield
    finally:
        await iop_client.aclose()
        flush()


# 数据更新（SFTP 拉取 → SQLite 入库）由 sftp_avl2db 每个 AVL 各一个独立进程负责
# （运行时数据目录为包内 runtime/）；
# 维表写入由 python -m local_excel2db 独立进程负责（只写，产物在包内 runtime/），
# 本进程不再读取维表：local_excel2db 的在线读取侧已下线，卖点映射恒为空。
# 本进程只读 sftp_avl2db 业务库，唯一写入是调用量统计
# （call_stat/runtime/call_sum.db，配置与产物都在 call_stat/ 包内）。
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


class IopNeed(Enum):
    """各业务对 IOP 标签的依赖。"""
    NONE = "none"              # 完全不取标签
    PORTRAIT = "portrait"      # 取标签并据此计算用户画像
    RAW_LABELS = "raw_labels"  # 只取原始标签，不做画像规则转换


def iop_need(product_id):
    """按产品 ID 声明标签依赖，业务归属由本文件的路由表派生。

    控损话术只由数据库字段拼装，不依赖用户标签，声明为 NONE；商机回捞
    需要标签拼装用户画像；其余产品 ID（诊断新线 / 兜底）只取原始标签、
    不做画像规则转换。诊断新线当前输出尚未消费标签，属已确认的预留。
    """
    if product_id in OPPORTUNITY_RECALL_PRODUCT_IDS:
        return IopNeed.PORTRAIT
    if product_id in LOSS_CONTROL_PRODUCT_IDS:
        return IopNeed.NONE
    return IopNeed.RAW_LABELS


class IopLabelProvider:
    """一次请求内的标签惰性提供者，负责同请求只查一次并隔离不同请求。

    实例必须在请求处理函数内创建：标签属于单个手机号，放到模块级或
    app.state 缓存会把上一个用户的标签带给下一个用户，且不会报错。
    """

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
        return self._labels


@app.post(MARKET_ASSISTANT_URL)
async def marketAssistant(args: Args):
    # 标签提供者按请求创建：标签属于单个手机号，不能放在模块级或 app.state
    # 缓存，否则会把上一个用户的标签带给下一个用户且不会报错
    label_provider = IopLabelProvider(args.telnum)
    # 画像按 product_id 在一次请求内共享：同一 ID 重复出现只拼一次。
    # 只在请求处理函数内创建，避免把某个用户的画像串给下一个用户
    portrait_cache = {}

    # 2. 建立产品对象列表
    # 维表在线读取侧已下线：卖点映射固定为空 dict（等价于原「无维表」降级），
    # 仍按原有参数形状传给每个产品对象，不改业务对象接口
    product_selling_point_match_dict = {}
    product_obj_list = []
    for one_product_obj in args.productId:
        # 逐产品 ID 声明标签依赖；提供者保证整请求最多查一次 IOP。
        # 逐个判断等价于对全部 ID 取并集，不会因首个 ID 不需要而漏查
        need = iop_need(one_product_obj.id)
        custom_label_info = (await label_provider.labels()
                             if need != IopNeed.NONE else {})
        # 只有声明 PORTRAIT 的业务需要画像；其余传 None，连拼装都不发生
        portrait_cache_for_obj = portrait_cache if need == IopNeed.PORTRAIT else None

        # 1. 特殊业务
        # 号卡业务、套餐业务、宽带业务（product_id → AVL 见本文件顶部路由表）
        if one_product_obj.id in OPPORTUNITY_RECALL_PRODUCT_IDS:
            avl = OPPORTUNITY_RECALL_AVLS[one_product_obj.id]
            record = await data_query.query(avl, args.telnum)
            product_obj_list.append(ProductInfoBySpec9554(one_product_obj.id, custom_label_info,
                                                          product_selling_point_match_dict, record,
                                                          portrait_cache_for_obj))
            continue

        # 2. 数据库中业务(控损专用)
        if one_product_obj.id in LOSS_CONTROL_PRODUCT_IDS:
            avl = LOSS_CONTROL_AVLS[one_product_obj.id]
            record = await data_query.query(avl, args.telnum)
            if record:
                product_obj_list.append(ProductInfoBySpec9591(one_product_obj.id, custom_label_info,
                                                              product_selling_point_match_dict, record,
                                                              portrait_cache_for_obj))
                continue

        # 3. 诊断新线
        # product_id 命中 F09629 即走诊断：按 BUSINESS_TYPE 关联产品场景 /
        # 营销 / 挽留话术，按手机号取用户场景做交集，再按场景优先级取切入话术。
        # 六项输出全为空时 build_diagnosis_record 返回 None，继续落兜底。
        diagnosis_record = await build_diagnosis_record(one_product_obj.id,
                                                        args.telnum)
        if diagnosis_record:
            product_obj_list.append(ProductInfoByDiagnosis(
                one_product_obj.id, custom_label_info,
                product_selling_point_match_dict, diagnosis_record,
                portrait_cache_for_obj))
            continue

        # 4. 兜底：不属于商机回捞 / 控损，且产品ID 未命中 F09629
        product_obj_list.append(ProductInfoRaw(one_product_obj.id, custom_label_info,
                                               product_selling_point_match_dict,
                                               portrait_cache_for_obj))

    # 3. 输出话术
    # 商机回捞 / 控损 / 诊断新线三类业务对象返回 code=0；其余兜底 code=2。
    # 这里按对象类型判定，不能靠 product_name 是否为空：兜底对象也可能因
    # 维表残留拿到非空名称，只看它会返回 code=0 + 空话术，看起来成功、实际无内容。
    allScripts = []
    for product_obj in product_obj_list:
        my_log.info(product_obj)
        if isinstance(product_obj, (ProductInfoBySpec9554, ProductInfoBySpec9591,
                                    ProductInfoByDiagnosis)):
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
