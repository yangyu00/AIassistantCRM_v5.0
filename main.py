from typing import List

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator

from IOP.get_custinfo import get_custom_info_in_global
from config import ImmutableYamlConfig
from excel2consts.dim_reader import load_product_selling_point
from services.product_info_raw import ProductInfoRaw as ProductInfoRaw
from services.rewrite_by_excel.rewrite import ProductInfoRewrite as ProductInfoByExcel
from services.rewrite_by_spec_f09554.rewrite import ProductInfoRewrite as ProductInfoBySpec9554
from services.rewrite_by_spec_f09591.rewrite import ProductInfoRewrite as ProductInfoBySpec9591
from services.rewrite_by_spec_f09609.rewrite import ProductInfoRewrite as ProductInfoBySpec9609
from sftp_workdir_f09554 import data_query as data_query_f09554
from sftp_workdir_f09591 import data_query as data_query_f09591
from sftp_workdir_f09609 import data_query as data_query_f09609
from src.call_stat import record_call
from src.exceptions import telnumError, productIdError, register_errors_handlers
from src.log_util import my_log

# 数据更新（SFTP 拉取 → SQLite 入库）由 tasks/ 下的 3 个独立进程负责；
# 维表更新由 excel2consts.dim_watcher 独立进程负责；
# 本进程只处理在线查询，所有维表与业务数据一律只读 SQLite。
app = FastAPI()
register_errors_handlers(app)
global_config = ImmutableYamlConfig()


@app.middleware("http")
async def count_call_middleware(request, call_next):
    """每次访问服务，当日调用量 +1（无论请求成功或失败都计数）。"""
    try:
        response = await call_next(request)
    finally:
        record_call()
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
        # 号卡业务、套餐业务、宽带业务
        if one_product_obj.id in ["202608041615335173", "202608041615335172", "202608041615335174"]:
            record = await data_query_f09554.get_latest_record(args.telnum, one_product_obj.id)
            product_obj_list.append(ProductInfoBySpec9554(one_product_obj.id, custom_label_info, record))
            continue

        # 2. 数据库中业务(控损专用)
        if one_product_obj.id in ["202609180955095402"]:
            record = await data_query_f09591.get_latest_record(args.telnum)
            if record:
                product_obj_list.append(ProductInfoBySpec9591(one_product_obj.id, custom_label_info, record))
                continue

        # 数据库中业务(客户诊断)
        record = await data_query_f09609.get_record(args.telnum, one_product_obj.id)
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


if __name__ == "__main__":
    uvicorn.run(app="main:app", **global_config['fastapi'])
