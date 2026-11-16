import datetime
import re
from contextlib import asynccontextmanager
from typing import List, Optional

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator

from IOP.get_custinfo import get_custom_info_in_global
from IOP.http_session import init_session, close_session
from config import ImmutableYamlConfig
from excel2consts.dim_reader import load_product_selling_point
from services.product_info_raw import ProductInfoRaw as ProductInfoRaw
from services.rewrite_by_excel.rewrite import ProductInfoRewrite as ProductInfoByExcel
from services.rewrite_by_spec_f09554.rewrite import ProductInfoRewrite as ProductInfoBySpec9554
from services.rewrite_by_spec_f09591.rewrite import ProductInfoRewrite as ProductInfoBySpec9591
from services.rewrite_by_spec_f09609.rewrite import ProductInfoRewrite as ProductInfoBySpec9609
from services import data_query
from src import call_stat
from src.exceptions import telnumError, productIdError, register_errors_handlers
from src.log_util import my_log

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时恢复调用量基数并创建 IOP 共享 ClientSession，关闭时收尾。

    IOP 会话必须在同一个 event loop 内创建/关闭，故挂在 lifespan 而非模块级
    或 atexit（atexit 拿不到运行中的 loop）。uvicorn 的 SIGTERM 退出路径不经过
    atexit，由 yield 之后显式收尾；src.call_stat 仍注册 atexit 作为非 ASGI
    运行方式的兜底，两处幂等。startup 里不做任何定时任务，只做一次性初始化。
    """
    base = call_stat.startup()
    my_log.info(f"调用量统计已恢复：当天已累计 {base} 次（重启后继续计数）")
    await init_session()
    try:
        yield
    finally:
        await close_session()
        call_stat.flush()


# 数据更新（SFTP 拉取 → SQLite 入库）由 tasks/ 下的 3 个独立进程负责；
# 维表更新由 excel2consts.dim_watcher 独立进程负责；
# 本进程对业务库与维表一律只读 SQLite，唯一写入是调用量统计（log/call_sum.db）。
app = FastAPI(lifespan=lifespan)
register_errors_handlers(app)
global_config = ImmutableYamlConfig()


@app.middleware("http")
async def count_call_middleware(request, call_next):
    """每次访问服务，当日调用量在内存 +1（无论请求成功或失败都计数）。

    累计到 call_stat.flush_threshold 次才由 src.call_stat 写一次 SQLite。
    """
    try:
        response = await call_next(request)
    finally:
        call_stat.record_call()
    return response


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


@app.post(str(global_config['marketAssistant']['url']))
async def marketAssistant(args: Args):
    # 1. 根据 手机号 获取 用户标签
    try:
        custom_label_info = await get_custom_info_in_global(args.telnum)
    except Exception as e:
        my_log.error(e)
        custom_label_info = {}

    # 2. 建立产品对象列表
    # 维表路由判断：一次请求只读一次，循环内复用
    product_selling_point_match_dict = load_product_selling_point()
    product_obj_list = []
    for one_product_obj in args.productId:
        # 1. 特殊业务
        # 号卡业务、套餐业务、宽带业务（ID 与库表映射见 tasks/config.py）
        if one_product_obj.id in data_query.OPPORTUNITY_RECALL_PRODUCT_IDS:
            record = await data_query.get_opportunity_recall_latest(args.telnum, one_product_obj.id)
            product_obj_list.append(ProductInfoBySpec9554(one_product_obj.id, custom_label_info, record))
            continue

        # 2. 数据库中业务(控损专用)
        if one_product_obj.id in data_query.LOSS_CONTROL_PRODUCT_IDS:
            record = await data_query.get_loss_control_latest(args.telnum)
            if record:
                product_obj_list.append(ProductInfoBySpec9591(one_product_obj.id, custom_label_info, record))
                continue

        # 数据库中业务(客户诊断)
        record = await data_query.get_customer_diagnosis_record(args.telnum, one_product_obj.id)
        if record:
            product_obj_list.append(ProductInfoBySpec9609(one_product_obj.id, custom_label_info, record))
            continue

        # 4. 维表中业务
        if one_product_obj.id in product_selling_point_match_dict:
            product_obj_list.append(ProductInfoByExcel(one_product_obj.id, custom_label_info))
            continue

        product_obj_list.append(ProductInfoRaw(one_product_obj.id, custom_label_info))

    # 3. 输出话术
    allScripts = []
    for product_obj in product_obj_list:
        my_log.info(product_obj)
        if product_obj.product_name is not None:
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


def _is_valid_date(value: str) -> bool:
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    try:
        datetime.datetime.strptime(value, '%Y-%m-%d')
        return True
    except ValueError:
        return False


def _is_valid_month(value: str) -> bool:
    if not re.fullmatch(r'\d{4}-\d{2}', value):
        return False
    return 1 <= int(value[5:7]) <= 12


@app.get(str(global_config.get('call_stat', {}).get('url', '/api/callStat')))
async def call_stat_query(date: Optional[str] = None, month: Optional[str] = None):
    """查询调用量：date=YYYY-MM-DD 返回单日；month=YYYY-MM 返回该月每天。

    两个参数必须且只能提供一个。返回完整累计调用量（含尚未落库的部分）。
    """
    if (date is None) == (month is None):
        return {"res_code": 1,
                "res_desc": "请且仅提供一个参数：date(YYYY-MM-DD) 或 month(YYYY-MM)",
                "result": ""}

    if date is not None:
        if not _is_valid_date(date):
            return {"res_code": 1,
                    "res_desc": "date 格式不正确，应为 YYYY-MM-DD，如 2026-11-16",
                    "result": ""}
        return {"res_code": 0, "res_desc": "",
                "result": {"date": date, "callCount": call_stat.get_call_count(date)}}

    if not _is_valid_month(month):
        return {"res_code": 1,
                "res_desc": "month 格式不正确，应为 YYYY-MM，如 2026-11",
                "result": ""}
    return {"res_code": 0, "res_desc": "",
            "result": {"month": month,
                       "days": [{"date": d, "callCount": c}
                                for d, c in call_stat.get_month_counts(month)]}}


if __name__ == "__main__":
    uvicorn.run(app="main:app", **global_config['fastapi'])
