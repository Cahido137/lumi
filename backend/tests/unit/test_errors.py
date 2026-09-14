"""业务异常基类、错误码载荷与全局异常处理器的单元测试"""

import ast
import json
import re
from pathlib import Path
from typing import Any

import app.utils.errors as errors_module
import app.utils.exception as exception_module
import httpx
import pytest
from app.config import OpsSettings
from app.schemas.auth import LoginRequest
from app.schemas.chat import ChatRequest
from app.schemas.error_code import ErrorCode, SessionErrorCode, WorkspaceErrorCode
from app.utils.errors import ConflictError, Error, NotFoundError, WorkspaceViolation
from app.utils.exception import (
    MAX_FIELD_ERRORS,
    _format_loc,
    business_error_handler,
    general_exception_handler,
    integrity_error_handler,
    request_validation_handler,
    sqlalchemy_error_handler,
)
from app.utils.exception_handlers import register_exception_handlers
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.requests import Request


def _payload(response: Any) -> dict:
    """把响应体解析为信封字典。

    Args:
        response: 处理器或测试客户端返回的响应对象。

    Returns:
        dict: 解析后的三段式信封。

    Note:
        starlette 将 body 标注为 bytes | memoryview, 先包一层 bytes 收敛类型,
        避免每个断言处都重复处理联合类型。
    """
    return json.loads(bytes(response.body))


def _make_request(path: str = "/api/test") -> Request:
    """构造一个最小可用的 HTTP Request, 仅供处理器读取 url.path"""
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
        "app": None,
    }
    return Request(scope)


def _patch_debug(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    """替换异常处理器模块读到的运维配置, 控制调试信息开关"""
    monkeypatch.setattr(exception_module, "get_opssettings", lambda: OpsSettings(debug_error_detail=enabled))


def _build_app():
    """构造一个只挂载异常处理器的最小应用, 用于验证注册是否生效

    Note:
        这里刻意用 httpx.ASGITransport 驱动应用, 不使用 fastapi.testclient:
        后者在模块级引用了已废弃的 anyio.abc.BlockingPortal 别名, 导入即产生 DeprecationWarning。
    """
    from fastapi import FastAPI

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/conflict")
    async def conflict():
        """抛出业务异常, 由 ExceptionMiddleware 命中业务处理器"""
        raise ConflictError("该会话已有正在进行的运行", code=SessionErrorCode.RUN_IN_PROGRESS)

    @app.get("/db-error")
    async def db_error():
        """抛出数据库异常, 由 ExceptionMiddleware 命中数据库处理器"""
        raise SQLAlchemyError("connection reset by peer")

    return app


async def _get(app, path: str) -> httpx.Response:
    """通过 ASGITransport 发起一次进程内请求"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


# ── 基类与子类 ──────────────────────────────────────────────────────────────


def test_base_error_constructible():
    """业务异常基类应能直接构造, 并回落到类级默认值"""
    err = Error()
    assert err.message == "服务器内部错误"
    assert err.error_code == "internal_error"
    assert err.http_status == 500


def test_subclass_inherits_default_message_and_code():
    """子类未传参时应使用自身重写的类级默认值"""
    err = NotFoundError()
    assert err.message == "资源不存在"
    assert err.error_code == "not_found"
    assert err.http_status == 404


def test_message_argument_overrides_default():
    """显式传入的 message 应覆盖类级默认文案"""
    err = ConflictError("该会话存在未完成的审批")
    assert err.message == "该会话存在未完成的审批"


def test_code_argument_overrides_class_default():
    """构造参数 code 应覆盖类级默认错误码, 用于同一异常类下细分场景"""
    err = ConflictError("已有活动运行", code=SessionErrorCode.RUN_IN_PROGRESS)
    assert err.error_code == "run_in_progress"
    assert ConflictError().error_code == "conflict"  # 类级默认值不被实例修改污染


def test_str_of_exception_is_message():
    """异常的字符串形式应为 message, 保证日志与 str(e) 可读"""
    assert str(WorkspaceViolation("路径越界")) == "路径越界"


def test_all_business_errors_derive_from_base():
    """全部业务异常都应继承自基类, 保证被同一个处理器捕获"""
    for err in (NotFoundError(), ConflictError(), WorkspaceViolation(), Error()):
        assert isinstance(err, Error)
        assert isinstance(err, Exception)


def test_detail_is_copied_not_aliased():
    """构造时传入的 detail 字典应被复制, 调用方后续修改不影响异常"""
    raw = {"path": "a"}
    err = WorkspaceViolation(detail=raw)
    raw["path"] = "b"
    assert err.detail == {"path": "a"}


def test_detail_defaults_to_empty_dict():
    """未传 detail 时应为空字典而不是 None"""
    assert NotFoundError().detail == {}


# ── to_payload ─────────────────────────────────────────────────────────────


def test_payload_contains_error_code():
    """载荷默认至少包含 error_code"""
    assert NotFoundError().to_payload() == {"error_code": "not_found"}


def test_payload_flattens_detail():
    """detail 中的键值对应被平铺进载荷"""
    err = WorkspaceViolation("路径越界", code=WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE, detail={"path": "../x"})
    assert err.to_payload() == {"error_code": "path_outside_workspace", "path": "../x"}


def test_payload_detail_overrides_error_code():
    """detail 中与 error_code 同名的键会覆盖前者"""
    err = NotFoundError(detail={"error_code": "overridden"})
    assert err.to_payload() == {"error_code": "overridden"}


def test_payload_hides_debug_by_default():
    """默认不附加异常类型名"""
    assert "error_type" not in NotFoundError().to_payload()


def test_payload_includes_debug_on_demand():
    """开启调试开关时载荷附带异常类型名"""
    assert NotFoundError().to_payload(include_debug=True)["error_type"] == "NotFoundError"


# ── 处理器: 业务异常 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("err", "expected_status"),
    [
        (NotFoundError(), 404),
        (ConflictError(), 409),
        (WorkspaceViolation(), 400),
        (Error(), 500),
    ],
)
async def test_business_handler_uses_exception_http_status(err, expected_status):
    """处理器应使用异常自身声明的 HTTP 状态码"""
    response = await business_error_handler(_make_request(), err)
    assert response.status_code == expected_status


async def test_business_handler_envelope_shape(monkeypatch):
    """业务异常响应应为三段式信封, data 中携带稳定错误码"""
    _patch_debug(monkeypatch, enabled=False)
    err = ConflictError("该会话存在未完成的审批", code=SessionErrorCode.PENDING_APPROVAL_EXISTS)
    response = await business_error_handler(_make_request("/api/sessions/x/chat"), err)
    assert _payload(response) == {
        "code": 409,
        "message": "该会话存在未完成的审批",
        "data": {"error_code": "pending_approval_exists"},
    }


async def test_business_handler_hides_stack_when_debug_off(monkeypatch):
    """调试开关关闭时, 业务异常响应不得携带异常类型与堆栈"""
    _patch_debug(monkeypatch, enabled=False)
    payload = _payload(await business_error_handler(_make_request(), NotFoundError()))["data"]
    assert "error_type" not in payload
    assert "traceback" not in payload


async def test_business_handler_exposes_type_when_debug_on(monkeypatch):
    """调试开关开启时, 业务异常响应附带异常类型名"""
    _patch_debug(monkeypatch, enabled=True)
    payload = _payload(await business_error_handler(_make_request(), NotFoundError()))["data"]
    assert payload["error_type"] == "NotFoundError"
    assert payload["error_code"] == "not_found"


# ── 处理器: 完整性约束 ──────────────────────────────────────────────────────


def _integrity_error(raw_message: str) -> IntegrityError:
    """构造一个带指定驱动层原始信息的 IntegrityError"""
    return IntegrityError("INSERT INTO users ...", None, Exception(raw_message))


@pytest.mark.parametrize(
    ("raw_message", "expected_code", "expected_message"),
    [
        ('duplicate key value violates unique constraint "users_username_key"', "duplicate", "数据已存在"),
        ("ERROR:  unique constraint violation", "duplicate", "数据已存在"),
        ("insert or update on table violates foreign key constraint", "foreign_key_violation", "关联数据不存在"),
        ("some other database level problem", "integrity_error", "数据约束冲突"),
    ],
)
async def test_integrity_handler_maps_error_code(raw_message, expected_code, expected_message):
    """完整性约束异常应按驱动层信息分流为稳定的错误码与中文文案"""
    response = await integrity_error_handler(_make_request(), _integrity_error(raw_message))
    body = _payload(response)
    assert response.status_code == 400
    assert body["code"] == 400
    assert body["message"] == expected_message
    assert body["data"]["error_code"] == expected_code


async def test_integrity_handler_keeps_error_code_when_debug_on(monkeypatch):
    """调试开关开启时, error_code 仍必须保留, 不能只返回调试信息"""
    _patch_debug(monkeypatch, enabled=True)
    body = _payload(await integrity_error_handler(_make_request(), _integrity_error("duplicate key value")))
    assert body["data"]["error_code"] == "duplicate"
    assert body["data"]["error_type"] == "IntegrityError"


async def test_integrity_handler_hides_raw_message_when_debug_off(monkeypatch):
    """调试开关关闭时, 不得把驱动层原始报错(含表名与约束名)返回给客户端"""
    _patch_debug(monkeypatch, enabled=False)
    raw = 'duplicate key value violates unique constraint "users_pkey"'
    body = _payload(await integrity_error_handler(_make_request(), _integrity_error(raw)))
    assert body["data"] == {"error_code": "duplicate"}
    assert "users_pkey" not in json.dumps(body, ensure_ascii=False)


# ── 处理器: 数据库异常与兜底异常 ─────────────────────────────────────────────


async def test_sqlalchemy_handler_returns_500_without_debug(monkeypatch):
    """调试关闭时数据库异常统一返回 500, data 为空"""
    _patch_debug(monkeypatch, enabled=False)
    response = await sqlalchemy_error_handler(_make_request(), SQLAlchemyError("connection reset"))
    body = _payload(response)
    assert response.status_code == 500
    assert body == {"code": 500, "message": "数据库操作错误", "data": None}


async def test_sqlalchemy_handler_captures_live_traceback(monkeypatch):
    """调试开启时数据库异常应带上真实堆栈, 而非空的 traceback 文本"""
    _patch_debug(monkeypatch, enabled=True)
    try:
        raise SQLAlchemyError("connection reset by peer")
    except SQLAlchemyError as exc:
        response = await sqlalchemy_error_handler(_make_request(), exc)
    data = _payload(response)["data"]
    assert data["error_type"] == "SQLAlchemyError"
    assert "connection reset by peer" in data["traceback"]


async def test_general_handler_returns_500_without_debug(monkeypatch):
    """调试关闭时兜底异常返回 500, data 为空"""
    _patch_debug(monkeypatch, enabled=False)
    response = await general_exception_handler(_make_request(), RuntimeError("boom"))
    body = _payload(response)
    assert response.status_code == 500
    assert body == {"code": 500, "message": "服务器内部错误", "data": None}


async def test_general_handler_captures_live_traceback(monkeypatch):
    """调试开启时兜底异常应带上真实堆栈"""
    _patch_debug(monkeypatch, enabled=True)
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        response = await general_exception_handler(_make_request(), exc)
    data = _payload(response)["data"]
    assert data["error_type"] == "RuntimeError"
    assert "boom" in data["traceback"]


# ── 注册与端到端信封 ────────────────────────────────────────────────────────


def test_register_maps_business_error_and_fallback():
    """注册后应用应同时挂载业务异常处理器与 Exception 兜底处理器"""
    from fastapi import FastAPI

    app = FastAPI()
    register_exception_handlers(app)
    assert app.exception_handlers[Error] is business_error_handler
    assert app.exception_handlers[Exception] is general_exception_handler
    assert app.exception_handlers[SQLAlchemyError] is sqlalchemy_error_handler
    assert app.exception_handlers[IntegrityError] is integrity_error_handler


async def test_subclass_hits_business_handler_end_to_end(monkeypatch):
    """端到端: Error 的子类应按 MRO 命中业务处理器, 而不是 Exception 兜底处理器"""
    _patch_debug(monkeypatch, enabled=False)
    response = await _get(_build_app(), "/conflict")
    assert response.status_code == 409
    assert response.json() == {
        "code": 409,
        "message": "该会话已有正在进行的运行",
        "data": {"error_code": "run_in_progress"},
    }


async def test_sqlalchemy_handler_can_format_traceback_inside_middleware(monkeypatch):
    """端到端: 经 ExceptionMiddleware 调用时 traceback.format_exc() 仍能取到当前异常

    Note:
        ExceptionMiddleware 在 except 块内 await 处理器, 因此 sys.exc_info() 仍然有效。
        若改用线程池执行同步处理器, 本断言会失败, 这也是处理器必须保持 async 的原因。
    """
    _patch_debug(monkeypatch, enabled=True)
    response = await _get(_build_app(), "/db-error")
    assert response.status_code == 500
    body = response.json()
    assert body["message"] == "数据库操作错误"
    assert body["data"]["error_type"] == "SQLAlchemyError"
    assert "connection reset by peer" in body["data"]["traceback"]


# ── 处理器: 请求校验失败 ─────────────────────────────────────────────────────

_PASSWORD_ERROR = {
    "type": "string_too_long",
    "loc": ("body", "password"),
    "msg": "String should have at most 72 characters",
    "input": "P@ssw0rd-Secret-明文",
    "ctx": {"max_length": 72},
}
"""构造校验异常用的单条错误项。

Note:
    形态与登录接口密码超长时 pydantic 的实际产出一致,
    其中 input 是用户提交的明文密码, 用于验证它不会出现在响应里。
"""


def _validation_error(errors: list) -> RequestValidationError:
    """构造一个带指定错误列表的请求校验异常"""
    return RequestValidationError(errors)


def _build_validation_app():
    """构造挂载真实请求模型的校验应用, 用于端到端验证 422 信封

    Note:
        与 _build_app 分开, 避免为校验用例给业务异常应用挂上无关路由。
        RequestValidationError 由 ExceptionMiddleware 处理且处理后不重新抛出,
        因此可以直接用 httpx.ASGITransport 读到响应体。
    """
    from fastapi import FastAPI, Query

    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/login")
    async def login(body: LoginRequest):
        """真实登录请求体, 密码长度上限 72"""
        return {"ok": True}

    @app.post("/chat")
    async def chat(body: ChatRequest):
        """真实对话请求体, content 长度上限 8000"""
        return {"ok": True}

    @app.get("/messages")
    async def messages(page_size: int = Query(20, alias="pageSize", ge=1, le=100)):
        """带查询参数约束的路由, 用于验证 loc 的 query 前缀"""
        return {"ok": page_size}

    return app


async def _request(app, method: str, path: str, **kwargs) -> httpx.Response:
    """通过 ASGITransport 对指定应用发起一次进程内请求"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.parametrize(
    ("loc", "expected"),
    [
        (("body", "content"), "body.content"),
        (("query", "pageSize"), "query.pageSize"),
        (("body", "items", 3, "name"), "body.items[3].name"),
        (("body", 1), "body[1]"),  # 请求体不是合法 JSON 时 pydantic 给的是字符偏移量
        ((), "(unknown)"),
    ],
)
def test_format_loc_normalizes_int_segments(loc, expected):
    """loc 中的整数应写成下标形式, 不能与对象键名混在一起"""
    assert _format_loc(loc) == expected


async def test_validation_handler_envelope_shape(monkeypatch):
    """校验失败应返回三段式信封, data 中携带稳定错误码与字段列表"""
    _patch_debug(monkeypatch, enabled=False)
    response = await request_validation_handler(_make_request("/api/auth/login"), _validation_error([_PASSWORD_ERROR]))
    body = _payload(response)
    assert response.status_code == 422
    assert body["code"] == 422
    assert body["message"] == "请求参数校验失败"
    assert body["data"] == {
        "error_code": "validation_error",
        "fields": [
            {
                "loc": "body.password",
                "type": "string_too_long",
                "msg": "String should have at most 72 characters",
            }
        ],
        "error_count": 1,
        "truncated": False,
    }


async def test_validation_handler_never_echoes_raw_input(monkeypatch):
    """调试关闭时, 响应中不得出现用户提交的原始值(此处为明文密码)"""
    _patch_debug(monkeypatch, enabled=False)
    response = await request_validation_handler(_make_request("/api/auth/login"), _validation_error([_PASSWORD_ERROR]))
    text = bytes(response.body).decode()
    assert "P@ssw0rd-Secret-明文" not in text
    assert "input" not in _payload(response)["data"]["fields"][0]


async def test_validation_handler_never_echoes_ctx_when_debug_off(monkeypatch):
    """调试关闭时, ctx 中的约束参数也不应出现"""
    _patch_debug(monkeypatch, enabled=False)
    response = await request_validation_handler(_make_request(), _validation_error([_PASSWORD_ERROR]))
    data = _payload(response)["data"]
    assert "ctx" not in data
    assert "max_length" not in bytes(response.body).decode()


async def test_validation_handler_exposes_raw_errors_when_debug_on(monkeypatch):
    """调试开启时才返回 pydantic 原始错误列表, 且 fields 中始终不含原始值"""
    _patch_debug(monkeypatch, enabled=True)
    response = await request_validation_handler(_make_request(), _validation_error([_PASSWORD_ERROR]))
    data = _payload(response)["data"]
    assert data["error_code"] == "validation_error"
    assert data["raw_errors"][0]["input"] == "P@ssw0rd-Secret-明文"
    assert data["fields"][0].get("input") is None


async def test_validation_handler_skips_non_dict_entries(monkeypatch):
    """非字典形态的错误项应被跳过, 而不是让处理器自身抛异常"""
    _patch_debug(monkeypatch, enabled=False)
    exc = RequestValidationError([_PASSWORD_ERROR, "not-a-dict", None])  # type: ignore[list-item]
    data = _payload(await request_validation_handler(_make_request(), exc))["data"]
    assert data["error_count"] == 1
    assert len(data["fields"]) == 1


async def test_validation_handler_truncates_field_list(monkeypatch):
    """字段错误超过上限时应截断, 但 error_count 仍报真实总数"""
    _patch_debug(monkeypatch, enabled=False)
    total = MAX_FIELD_ERRORS + 70
    many = [{"type": "missing", "loc": ("body", "items", i, "id"), "msg": "Field required"} for i in range(total)]
    data = _payload(await request_validation_handler(_make_request(), _validation_error(many)))["data"]
    assert len(data["fields"]) == MAX_FIELD_ERRORS
    assert data["error_count"] == total
    assert data["truncated"] is True
    assert data["fields"][-1]["loc"] == f"body.items[{MAX_FIELD_ERRORS - 1}].id"


def test_register_overrides_fastapi_default_validation_handler():
    """注册后应覆盖 FastAPI 自带的 422 处理器, 否则信封与回显策略都不生效"""
    from fastapi import FastAPI
    from fastapi.exception_handlers import request_validation_exception_handler

    app = FastAPI()
    register_exception_handlers(app)
    assert app.exception_handlers[RequestValidationError] is request_validation_handler
    assert app.exception_handlers[RequestValidationError] is not request_validation_exception_handler


async def test_login_password_too_long_returns_envelope_without_plaintext(monkeypatch):
    """端到端: 密码超长触发 422, 响应为三段式信封且整段响应不含明文密码"""
    _patch_debug(monkeypatch, enabled=False)
    plaintext = "P@ssw0rd-超長-" * 20
    payload = {"username": "alice", "password": plaintext}
    response = await _request(_build_validation_app(), "POST", "/login", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 422
    assert body["message"] == "请求参数校验失败"
    assert body["data"]["error_code"] == "validation_error"
    assert body["data"]["fields"][0]["loc"] == "body.password"
    assert plaintext not in response.text
    assert "P@ssw0rd" not in response.text


async def test_chat_content_too_long_does_not_echo_payload(monkeypatch):
    """端到端: content 超过 8000 字符触发 422, 且不把超长入参原样吐回"""
    _patch_debug(monkeypatch, enabled=False)
    response = await _request(_build_validation_app(), "POST", "/chat", json={"content": "长" * 8001})
    assert response.status_code == 422
    data = response.json()["data"]
    assert data["fields"][0]["type"] == "string_too_long"
    assert len(response.text) < 2000  # 入参有 8001 个字符, 响应远小于它说明没有被回显


async def test_invalid_json_body_uses_bracket_loc(monkeypatch):
    """端到端: 请求体不是合法 JSON 时, loc 的整数偏移量应写成下标形式"""
    _patch_debug(monkeypatch, enabled=False)
    response = await _request(
        _build_validation_app(),
        "POST",
        "/chat",
        content=b"{bad json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    fields = response.json()["data"]["fields"]
    assert fields[0]["type"] == "json_invalid"
    assert fields[0]["loc"].startswith("body[")


async def test_query_param_violation_uses_query_loc_prefix(monkeypatch):
    """端到端: 查询参数越界时 loc 应带 query 前缀"""
    _patch_debug(monkeypatch, enabled=False)
    response = await _request(_build_validation_app(), "GET", "/messages", params={"pageSize": 999})
    assert response.status_code == 422
    data = response.json()["data"]
    assert data["error_code"] == "validation_error"
    assert data["fields"][0]["loc"] == "query.pageSize"


# ── 错误码注册表 ────────────────────────────────────────────────────────────


def _all_error_codes() -> list[ErrorCode]:
    """收集全部已登记的错误码成员。

    Returns:
        list[ErrorCode]: 所有 ErrorCode 子类中的成员。

    Note:
        ErrorCode 是不含成员的基类, 无法直接迭代, 只能逐个遍历子类收集。
    """
    return [member for group in ErrorCode.__subclasses__() for member in group]


def test_error_code_registry_is_not_empty():
    """注册表收集逻辑本身要有效, 否则后续几条断言会退化为恒真"""
    assert len(_all_error_codes()) >= 30


def test_error_code_values_are_unique():
    """错误码取值全局唯一: 两个概念不得共用同一个码"""
    values = [member.value for member in _all_error_codes()]
    duplicated = sorted({value for value in values if values.count(value) > 1})
    assert duplicated == []


def test_error_code_values_are_snake_case():
    """错误码取值统一为小写下划线形式, 这是对客户端公布的契约"""
    for member in _all_error_codes():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", member.value), member.value


def test_error_code_serializes_as_plain_string():
    """错误码经 JSON 序列化后必须是裸值, 不能变成 ErrorCode.XXX 形式"""
    member = _all_error_codes()[0]
    assert json.dumps({"error_code": member}) == f'{{"error_code": "{member.value}"}}'


def test_exception_default_codes_are_registered():
    """每个业务异常类的类级默认码都必须是已登记的错误码成员"""
    registered = set(_all_error_codes())
    classes = [obj for obj in vars(errors_module).values() if isinstance(obj, type) and issubclass(obj, Error)]
    assert len(classes) >= 10
    for cls in classes:
        assert cls.code in registered, f"{cls.__name__}.code={cls.code!r} 未登记"


def test_no_bare_error_code_strings_in_app():
    """app/ 内不允许出现与错误码取值相同的裸字符串, 定义文件本身除外"""
    values = {member.value for member in _all_error_codes()}
    app_root = Path(__file__).resolve().parents[2] / "app"
    registry = app_root / "schemas" / "error_code.py"
    offenders = [
        f"{path.relative_to(app_root.parent)}:{node.lineno}: {node.value!r}"
        for path in sorted(app_root.rglob("*.py"))
        if path != registry
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in values
    ]
    assert offenders == []
