"""全局稳定错误码。"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """所有错误码的基类。"""


class CommonErrorCode(ErrorCode):
    """通用错误码。"""

    INTERNAL_ERROR = "internal_error"
    """未知的服务器内部错误。"""

    INVALID_REQUEST = "invalid_request"
    """请求参数非法, 或请求体不满足业务前置条件。"""

    VALIDATION_ERROR = "validation_error"
    """请求体、查询参数或路径参数的结构校验失败。"""

    UNAUTHORIZED = "unauthorized"
    """未登录或登录信息失效。"""

    FORBIDDEN = "forbidden"
    """无权访问资源或执行操作。"""

    NOT_FOUND = "not_found"
    """目标资源不存在, 或资源不属于该用户。"""

    CONFLICT = "conflict"
    """资源当前状态与请求冲突。"""

    RATE_LIMITED = "rate_limited"
    """触发频率限制或并发限制。"""

    PAYLOAD_TOO_LARGE = "payload_too_large"
    """请求体或工具产出体积过大。"""

    UPSTREAM_ERROR = "upstream_error"
    """模型供应商、搜索服务商等外部服务依赖不可用。"""


class IntegrityErrorCode(ErrorCode):
    """数据库完整性约束错误码。"""

    DUPLICATE = "duplicate"
    """唯一约束冲突: 数据已存在。"""

    FOREIGN_KEY_VIOLATION = "foreign_key_violation"
    """外键约束冲突: 关联数据不存在。"""

    INTEGRITY_ERROR = "integrity_error"
    """其他数据库完整性约束冲突。"""


class WorkspaceErrorCode(ErrorCode):
    """受限工作区错误码。"""

    WORKSPACE_VIOLATION = "workspace_violation"
    """受限工作区的访问策略被违反。"""

    WORKSPACE_NOT_CONFIGURED = "workspace_not_configured"
    """未配置工作区根目录。"""

    WORKSPACE_ROOT_INVALID = "workspace_root_invalid"
    """配置的工作区根目录不存在或不是目录。"""

    EMPTY_PATH = "empty_path"
    """传入的路径为空。"""

    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    """路径越出工作区根目录。"""

    PATH_DENIED = "path_denied"
    """路径命中拒绝访问的通配模式。"""

    NOT_A_FILE = "not_a_file"
    """目标不是普通文件。"""

    IS_DIRECTORY = "is_directory"
    """目标是目录, 不是可写入文件。"""

    BAD_ENCODING = "bad_encoding"
    """指定的编码不受支持。"""

    BAD_CONTENT = "bad_content"
    """内容无法以指定编码表示。"""

    READ_FAILED = "read_failed"
    """文件读取过程中发生操作系统级错误。"""

    MKDIR_FAILED = "mkdir_failed"
    """创建目录失败。"""

    WRITE_FAILED = "write_failed"
    """文件写入过程中发生操作系统级错误。"""


class SessionErrorCode(ErrorCode):
    """会话与审批状态冲突错误码。"""

    RUN_IN_PROGRESS = "run_in_progress"
    """该会话已有正在运行的图运行。"""

    PENDING_APPROVAL_EXISTS = "pending_approval_exists"
    """该会话存在尚未处理的审批单。"""


class NetworkErrorCode(ErrorCode):
    """出站网络策略错误码。"""

    NETWORK_POLICY_VIOLATION = "network_policy_violation"
    """出站网络策略被违反, 未细分具体原因时使用此错误码。"""

    NETWORK_DISABLED = "network_disabled"
    """出站网络访问被总开关关闭。"""

    SCHEME_DENIED = "scheme_denied"
    """URL 协议不在允许范围内。"""

    HOST_DENIED = "host_denied"
    """主机命中拒绝名单、缺失或无法解析成 URL。"""

    PORT_DENIED = "port_denied"
    """目标端口不在白名单内。"""

    ADDRESS_DENIED = "address_denied"
    """主机解析的地址因安全问题被拒绝。"""

    DNS_FAILED = "dns_failed"
    """主机名解析失败。"""

    TOO_MANY_REDIRECTS = "too_many_redirects"
    """重定向跳数超过策略上限。"""
