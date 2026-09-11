"""业务异常基类、错误码载荷与全局异常处理器的单元测试"""

import json

import app.utils.exception as exception_module
import httpx
import pytest
from app.config import OpsSettings
from app.utils.errors import ConflictError, Error, NotFoundError, WorkspaceViolation
from app.utils.exception import (
    business_error_handler,
    general_exception_handler,
    integrity_error_handler,
    sqlalchemy_error_handler,
)
from app.utils.exception_handlers import register_exception_handlers
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.requests import Request


def _make_request(path: str = "/api/test") -> Request:
    """构造一个最小可用的 HTTP Request, 仅供处理器读取 url.path"""
    scope = {
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
        raise ConflictError("该会话已有正在进行的运行", code="run_in_progress")

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
    err = ConflictError("已有活动运行", code="run_in_progress")
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
    err = WorkspaceViolation("路径越界", code="path_outside_workspace", detail={"path": "../x"})
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
    err = ConflictError("该会话存在未完成的审批", code="pending_approval_exists")
    response = await business_error_handler(_make_request("/api/sessions/x/chat"), err)
    assert json.loads(response.body) == {
        "code": 409,
        "message": "该会话存在未完成的审批",
        "data": {"error_code": "pending_approval_exists"},
    }


async def test_business_handler_hides_stack_when_debug_off(monkeypatch):
    """调试开关关闭时, 业务异常响应不得携带异常类型与堆栈"""
    _patch_debug(monkeypatch, enabled=False)
    payload = json.loads((await business_error_handler(_make_request(), NotFoundError())).body)["data"]
    assert "error_type" not in payload
    assert "traceback" not in payload


async def test_business_handler_exposes_type_when_debug_on(monkeypatch):
    """调试开关开启时, 业务异常响应附带异常类型名"""
    _patch_debug(monkeypatch, enabled=True)
    payload = json.loads((await business_error_handler(_make_request(), NotFoundError())).body)["data"]
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
    body = json.loads(response.body)
    assert response.status_code == 400
    assert body["code"] == 400
    assert body["message"] == expected_message
    assert body["data"]["error_code"] == expected_code


async def test_integrity_handler_keeps_error_code_when_debug_on(monkeypatch):
    """调试开关开启时, error_code 仍必须保留, 不能只返回调试信息"""
    _patch_debug(monkeypatch, enabled=True)
    body = json.loads((await integrity_error_handler(_make_request(), _integrity_error("duplicate key value"))).body)
    assert body["data"]["error_code"] == "duplicate"
    assert body["data"]["error_type"] == "IntegrityError"


async def test_integrity_handler_hides_raw_message_when_debug_off(monkeypatch):
    """调试开关关闭时, 不得把驱动层原始报错(含表名与约束名)返回给客户端"""
    _patch_debug(monkeypatch, enabled=False)
    raw = 'duplicate key value violates unique constraint "users_pkey"'
    body = json.loads((await integrity_error_handler(_make_request(), _integrity_error(raw))).body)
    assert body["data"] == {"error_code": "duplicate"}
    assert "users_pkey" not in json.dumps(body, ensure_ascii=False)


# ── 处理器: 数据库异常与兜底异常 ─────────────────────────────────────────────


async def test_sqlalchemy_handler_returns_500_without_debug(monkeypatch):
    """调试关闭时数据库异常统一返回 500, data 为空"""
    _patch_debug(monkeypatch, enabled=False)
    response = await sqlalchemy_error_handler(_make_request(), SQLAlchemyError("connection reset"))
    body = json.loads(response.body)
    assert response.status_code == 500
    assert body == {"code": 500, "message": "数据库操作错误", "data": None}


async def test_sqlalchemy_handler_captures_live_traceback(monkeypatch):
    """调试开启时数据库异常应带上真实堆栈, 而非空的 traceback 文本"""
    _patch_debug(monkeypatch, enabled=True)
    try:
        raise SQLAlchemyError("connection reset by peer")
    except SQLAlchemyError as exc:
        response = await sqlalchemy_error_handler(_make_request(), exc)
    data = json.loads(response.body)["data"]
    assert data["error_type"] == "SQLAlchemyError"
    assert "connection reset by peer" in data["traceback"]


async def test_general_handler_returns_500_without_debug(monkeypatch):
    """调试关闭时兜底异常返回 500, data 为空"""
    _patch_debug(monkeypatch, enabled=False)
    response = await general_exception_handler(_make_request(), RuntimeError("boom"))
    body = json.loads(response.body)
    assert response.status_code == 500
    assert body == {"code": 500, "message": "服务器内部错误", "data": None}


async def test_general_handler_captures_live_traceback(monkeypatch):
    """调试开启时兜底异常应带上真实堆栈"""
    _patch_debug(monkeypatch, enabled=True)
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        response = await general_exception_handler(_make_request(), exc)
    data = json.loads(response.body)["data"]
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
