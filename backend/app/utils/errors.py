"""业务异常基类与稳定错误码定义。

本模块集中声明了全部的业务异常, 路由与运行器会抛出这些异常。
"""

from typing import Any, ClassVar


class Error(Exception):
    """全部业务异常类的基类。"""

    code: ClassVar[str] = "internal_error"
    """类级默认错误码。"""

    http_status: ClassVar[int] = 500
    """类级默认 HTTP 状态码。"""

    default_message: ClassVar[str] = "服务器内部错误"
    """类级默认错误消息文案。"""

    def __init__(
        self, message: str | None = None, *, code: str | None = None, detail: dict[str, Any] | None = None
    ) -> None:
        """初始化业务异常。

        Args:
            message: 面向用户的错误说明文本。
            code: 覆盖类级默认错误码, 用于异常场景细分。
            detail: 附加的结构化上下文。
        """
        self.message: str = message or self.default_message
        self.error_code: str = code or self.code
        self.detail: dict[str, Any] = dict(detail or {})
        super().__init__(self.message)

    def to_payload(self, *, include_debug: bool = False) -> dict[str, Any]:
        """把异常转换为响应信封 data 字段的载荷。

        Args:
            include_debug: 是否附加异常类型名等调试信息。

        Returns:
            dict[str, Any]: 至少包含 error_code 的字典, detail 中的键值对会被平铺。

        Note:
            detail 中与 error_code 同名的键会覆盖前者, 因此不应在 detail 中再包含一份错误码。
        """
        payload: dict[str, Any] = {"error_code": self.error_code, **self.detail}
        if include_debug:
            payload["error_type"] = type(self).__name__
        return payload


class InvalidRequestError(Error):
    """请求参数非法, 或请求体不满足业务前置条件。"""

    code = "invalid_request"
    http_status = 400
    default_message = "请求参数非法"


class UnauthorizedError(Error):
    """未登录或登录信息失效。"""

    code = "unauthorized"
    http_status = 401
    default_message = "未登录或登录信息失效"


class ForbiddenError(Error):
    """无权访问该资源或执行该操作。"""

    code = "forbidden"
    http_status = 403
    default_message = "无权执行该操作"


class NotFoundError(Error):
    """目标资源不存在, 或资源不属于该用户。

    Note:
        归属校验失败应该抛出本异常。
    """

    code = "not_found"
    http_status = 404
    default_message = "资源不存在"


class ConflictError(Error):
    """资源当前状态与请求冲突。

    Note:
        存在未完成的审批、审批单已被处理等应该抛出本异常。
    """

    code = "conflict"
    http_status = 409
    default_message = "资源状态冲突"


class RateLimitedError(Error):
    """触发频率限制或并发限制。"""

    code = "rate_limited"
    http_status = 429
    default_message = "请求过于频繁, 请稍后重试"


class PayloadTooLargeError(Error):
    """请求内容或工具产物超出允许的体积上限。"""

    code = "payload_too_large"
    http_status = 413
    default_message = "内容超出允许的大小上限"


class UpstreamError(Error):
    """模型供应商、搜索服务商等外部依赖不可用。"""

    code = "upstream_error"
    http_status = 502
    default_message = "外部服务不可用"


class WorkspaceViolation(Error):
    """受限工作区的访问策略被违反。

    Note:
        路径越界、命中拒绝规则、超出字节上限等应抛出本异常。
    """

    code = "workspace_violation"
    http_status = 400
    default_message = "该操作超出受限工作区允许的范围或规则策略"


class NetworkPolicyViolation(Error):
    """网络出站访问规则策略被违反。

    Note:
        目标解析到内网地址、端口不在白名单、重定向到内网等应抛出本异常。
    """

    code = "network_policy_violation"
    http_status = 400
    default_message = "目标地址不在允许的网络访问范围内"
