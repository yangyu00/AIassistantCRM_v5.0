import datetime
import importlib
import os
import subprocess
import threading
from pathlib import Path
from typing import List

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator

from IOP.get_custinfo import get_custom_info_in_global
from config import ImmutableYamlConfig
from services.product_info_raw import ProductInfoRaw as ProductInfoRaw
from services.rewrite_by_excel.rewrite import ProductInfoRewrite as ProductInfoByExcel
from services.rewrite_by_spec_f09554.rewrite import ProductInfoRewrite as ProductInfoBySpec9554
from services.rewrite_by_spec_f09591.rewrite import ProductInfoRewrite as ProductInfoBySpec9591
from services.rewrite_by_spec_f09609.rewrite import ProductInfoRewrite as ProductInfoBySpec9609
from sftp_workdir_f09554 import data_query as data_query_f09554
from sftp_workdir_f09554.run import run_sftp_service_task as run_sftp_service_f09554_task
from sftp_workdir_f09591 import data_query as data_query_f09591
from sftp_workdir_f09591.run import run_sftp_service_task as run_sftp_service_f09591_task
from sftp_workdir_f09609 import data_query as data_query_f09609
from sftp_workdir_f09609.run import run_sftp_service_task as run_sftp_service_f09609_task
from src import consts
from src.call_stat import record_call
from src.exceptions import telnumError, productIdError, register_errors_handlers
from src.log_util import my_log
from src.task_scheduler import repeat_task

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


excel_update_time = []
first_launch = True

MAIN_PATH = Path(__file__).resolve().parent


@app.on_event('startup')
@repeat_task(seconds=60 * 60)
def repeat_update_consts() -> None:
    # 任务1.定时检查卖点维表和标签转换维表是否更新，更新则重写src中的consts文件，并利用importlib.reload(consts)实现热更新
    # 对应api中“维表中业务”
    global excel_update_time, first_launch
    heart_file = {
        f'{str(MAIN_PATH)}/excel2consts/卖点维表.xlsx': f'{str(MAIN_PATH)}/excel2consts/product_selling_point_match.py',
        f'{str(MAIN_PATH)}/excel2consts/标签转换维表.xlsx': f'{str(MAIN_PATH)}/excel2consts/labelName1_to_labelName2.py'
    }

    for i, (one_file_path, convert_script) in enumerate(heart_file.items()):
        mtime = os.path.getmtime(one_file_path)  # 获取文件更新时间
        mtime = datetime.datetime.fromtimestamp(mtime)  # 时间转为datetime格式

        if len(excel_update_time) < len(heart_file):
            excel_update_time.append(mtime)

        if mtime != excel_update_time[i] or first_launch:
            if len(excel_update_time) == len(heart_file):
                first_launch = False
            my_log.info(f'{os.path.basename(one_file_path)} 触发更新任务')
            try:
                if isinstance(convert_script, str):
                    cmd_list = ['python', f'{convert_script}']
                    subprocess.run(cmd_list, shell=False, capture_output=False, check=False)
                elif isinstance(convert_script, list):
                    for script in convert_script:
                        cmd_list = ['python', f'{script}']
                        subprocess.run(cmd_list, shell=False, capture_output=False, check=False)
            except Exception as e:
                my_log.error(f'UPDATE {os.path.basename(one_file_path)} ERROR')
            else:
                importlib.reload(consts)
                excel_update_time[i] = mtime

    # 任务2.定时异步运行 sftp_service 的任务函数
    # 对应api中“号卡业务、套餐业务、宽带业务”
    sftp_f09554_thread = threading.Thread(
        target=run_sftp_service_f09554_task,
        daemon=True  # 设置为守护线程
    )
    sftp_f09554_thread.start()

    # 对应api中“数据库中业务”
    sftp_f09591_thread = threading.Thread(
        target=run_sftp_service_f09591_task,
        daemon=True  # 设置为守护线程
    )
    sftp_f09591_thread.start()

    # 对应api中“数据库中业务”
    sftp_f09609_thread = threading.Thread(
        target=run_sftp_service_f09609_task,
        daemon=True  # 设置为守护线程
    )
    sftp_f09609_thread.start()


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
        if one_product_obj.id in consts.product_selling_point_match_dict.keys():
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
