from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.exceptions import RequestValidationError
from src.log_util import my_log


class telnumError(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return "电话号错误! 输入为: " + self.message


class productIdError(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return "产品ID错误! 输入为: " + self.message


def register_errors_handlers(app: FastAPI) -> None:

    @app.exception_handler(telnumError)
    async def validation_exception_handler(request: Request, exc: telnumError):
        my_log.error(str(exc))
        return JSONResponse({"res_code": 1, "res_desc": "电话号输入不正确", "result": ""},
                            status_code=status.HTTP_200_OK)

    @app.exception_handler(productIdError)
    async def validation_exception_handler(request: Request, exc: productIdError):
        req_json = await request.json()
        productId = req_json.get("productId", [])
        exc.message = str(productId)
        my_log.error(str(exc))
        return JSONResponse({"res_code": 1, "res_desc": "产品ID输入不正确", "result": ""},
                            status_code=status.HTTP_200_OK)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse({"res_code": 1, "res_desc": "参数校验错误", "result": str(exc)},
                            status_code=status.HTTP_200_OK)
