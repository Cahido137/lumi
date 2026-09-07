"""统一响应体封装。"""

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


def success_response(message: str = "success", data=None):
    """构造统一成功响应。

    Args:
        message: 响应提示消息, 默认为 success。
        data: 响应载荷。

    Returns:
        JSONResponse: 三段式响应信封 {"code", "message", "data"}。
    """
    content = {"code": 200, "message": message, "data": data}
    return JSONResponse(content=jsonable_encoder(content))
