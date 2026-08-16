from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

def success_response(message: str = "success", data = None):
    """统一成功响应"""
    content =  {
        "code": 200,
        "message": message,
        "data": data
    }
    return JSONResponse(content=jsonable_encoder(content))


def fail_response(status_code: int, message: str, data=None):
    """统一错误响应"""
    content = {
        "code": status_code,
        "message": message,
        "data": data
    }
    return JSONResponse(content=jsonable_encoder(content))