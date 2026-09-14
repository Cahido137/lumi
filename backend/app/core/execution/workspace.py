"""受限工作区的路径解析与读写限额。

工具入参中的路径会先经本模块解析。
"""

import fnmatch
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import get_workspacesettings
from app.schemas.error_code import CommonErrorCode, WorkspaceErrorCode
from app.utils.errors import WorkspaceViolation

_READ_CHUNK_BYTES = 64 * 1024
"""流式读取单次读入的字节数。"""


@dataclass(frozen=True)
class ReadResult:
    """一次受限读取的结果。"""

    text: str
    """解码后的文本内容, 可能被截断。"""

    byte_size: int
    """实际从磁盘读入的字节数。"""

    truncated_bytes: bool
    """是否因达到字节上限而停止读取。"""

    truncated_chars: bool
    """是否因达到字符上限而裁掉尾部文本。"""


@dataclass(frozen=True)
class WriteResult:
    """一次受限写入的结果。"""

    path: Path
    """实际写入的规范化绝对路径。"""

    byte_size: int
    """写入的字节数。"""

    created: bool
    """本次写入是否新建了文件 (False 表示进行了文件覆盖)"""


@dataclass(frozen=True)
class WorkspacePolicy:
    """一个工作区的访问策略。"""

    root: Path
    """工作区根目录。"""

    max_read_bytes: int = 2 * 1024 * 1024
    """单次读取的字节上限。"""

    max_write_bytes: int = 4 * 1024 * 1024
    """单次写入的字节上限。"""

    deny_patterns: tuple[str, ...] = ()
    """拒绝访问的通配模式, 同时匹配工作区相对路径与文件名。"""

    def __post_init__(self) -> None:
        """规范化根目录。"""

        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    def relative_posix(self, target: Path) -> str:
        """把工作区内的绝对路径转换为相对根目录的 POSIX 风格字符串。

        Args:
            target: 已确认位于工作区内的绝对路径。

        Returns:
            str: 相对根目录的路径字符串。

        Raises:
            ValueError: target 不在工作区内。
        """
        return target.relative_to(self.root).as_posix()

    def is_denied(self, target: Path) -> bool:
        """判断目标路径是否命中拒绝模式。

        Args:
            target: 已确认位于工作区内的绝对路径。

        Returns:
            bool: 命中任意模式时为 True。
        """
        rel = self.relative_posix(target)  # 相对路径
        name = target.name  # 基名形态
        return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in self.deny_patterns)


def resolve_within_workspace(policy: WorkspacePolicy, raw_path: str, *, must_exist: bool = False) -> Path:
    """把工具入参中的路径解析为工作区内的规范化绝对路径。

    Args:
        policy: 生效的工作区策略。
        raw_path: 工具入参中的路径。
        must_exist: 解析结果是否要求必须是已存在的普通文件。

    Returns:
        Path: 规范化后的绝对路径, 保证位于工作区内且未命中拒绝模式。

    Raises:
        WorkspaceViolation: 路径为空、越出工作区、命中拒绝模式、文件不存在、目标不是普通文件。
    """
    # 路径空值判定
    if not raw_path or not raw_path.strip():
        raise WorkspaceViolation("路径不能为空", code=WorkspaceErrorCode.EMPTY_PATH)

    candidate = Path(raw_path).expanduser()  # 展开路径中的～
    if not candidate.is_absolute():
        candidate = policy.root / candidate  # 相对路径拼接到工作区根目录形成绝对路径
    resolved = candidate.resolve()  # 消解..、跟随符号链接、补全为绝对路径

    # 归属判定
    if not resolved.is_relative_to(policy.root):
        raise WorkspaceViolation(
            message=f"路径越出工作区, 已拒绝: {raw_path}",
            code=WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE,
            detail={"path": raw_path, "workspace_root": str(policy.root)},
        )

    # 拒绝模式
    if policy.is_denied(resolved):
        raise WorkspaceViolation(
            message=f"路径命中拒绝规则, 已拒绝: {raw_path}",
            code=WorkspaceErrorCode.PATH_DENIED,
            detail={"path": raw_path},
        )

    # 类型与存在性判定
    if must_exist:
        # 文件存在性判断
        if not resolved.exists():
            raise WorkspaceViolation(
                message=f"指定文件不存在: {raw_path}", code=CommonErrorCode.NOT_FOUND, detail={"path": str(resolved)}
            )
        # 文件类型判断
        if not resolved.is_file():
            raise WorkspaceViolation(
                message=f"目标文件不是普通文件: {raw_path}",
                code=WorkspaceErrorCode.NOT_A_FILE,
                detail={"path": str(resolved)},
            )
    return resolved


def read_text_bounded(
    path: Path, *, encoding: str = "utf-8", max_bytes: int, max_chars: int | None = None
) -> ReadResult:
    """遵循字节上限分块读取文本文件。

    Args:
        path: 已通过策略解析的文件绝对路径。
        encoding: 文件使用的编码。
        max_bytes: 允许读入的最大字节数。
        max_chars: 允许回传的最大字符数, 为空表示不做字符级截断。

    Returns:
        ReadResult: 读取结果。

    Raises:
        WorkspaceViolation: 文件不可读或编码名非法。

    Note:
        本函数为同步阻塞 IO。
    """
    buffer = bytearray()  # 缓冲区
    truncated_bytes = False
    try:
        with Path(path).open("rb") as fh:
            # 循环读取文件
            while len(buffer) < max_bytes:
                chunk = fh.read(min(_READ_CHUNK_BYTES, max_bytes - len(buffer)))
                # 文件已读完
                if not chunk:
                    break
                buffer.extend(chunk)
                # 判断边界读取与最大字节的关系
                if len(buffer) >= max_bytes:
                    truncated_bytes = fh.read(1) != b""
                    break
    except OSError as e:
        raise WorkspaceViolation(
            f"文件读取失败: {e}", code=WorkspaceErrorCode.READ_FAILED, detail={"path": str(path)}
        ) from e

    try:
        text = bytes(buffer).decode(encoding, errors="replace")  # 替换二进制文件乱码字符, 让模型能读取到而不是抛出异常
    except LookupError as e:
        raise WorkspaceViolation(f"不支持的编码: {encoding}", code=WorkspaceErrorCode.BAD_ENCODING) from e

    truncated_chars = False
    # 分开返回字节上限与字符上限
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars]
        truncated_chars = True
    return ReadResult(
        text=text, byte_size=len(buffer), truncated_bytes=truncated_bytes, truncated_chars=truncated_chars
    )


def write_text_atomic(path: Path, content: str, *, encoding: str = "utf-8", max_bytes: int) -> WriteResult:
    """以同目录临时文件加原子替换的方式写入文本文件。

    Args:
        path: 已通过策略解析的绝对路径。
        content: 待写入的完整文本。
        encoding: 使用的编码名。
        max_bytes: 允许写入的最大字节数, 超出最大字节将拒绝写入。

    Returns:
        WriteResult: 写入结果。

    Raises:
        WorkspaceViolation: 目标路径是目录、内容超出字节上限、编码非法、父目录不可创建、写入失败。
    """
    try:
        # 进行编码
        data = content.encode(encoding)
    except LookupError as e:
        raise WorkspaceViolation(f"不支持的编码: {encoding}", code=WorkspaceErrorCode.BAD_ENCODING) from e
    except UnicodeEncodeError as e:
        raise WorkspaceViolation(f"内容无法以 {encoding} 编码", code=WorkspaceErrorCode.BAD_CONTENT) from e

    target = Path(path)
    # 判断目标路径是否是一个目录
    if target.is_dir():
        raise WorkspaceViolation(
            f"目标路径为目录, 不可写入: {target}", code=WorkspaceErrorCode.IS_DIRECTORY, detail={"path": str(target)}
        )
    # 写入上限判断
    if len(data) > max_bytes:
        raise WorkspaceViolation(
            message="写入内容超出字节上限",
            code=CommonErrorCode.PAYLOAD_TOO_LARGE,
            detail={"byte_size": len(data), "max_bytes": max_bytes},
        )

    created = not target.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise WorkspaceViolation(
            f"无法创建目录: {e}", code=WorkspaceErrorCode.MKDIR_FAILED, detail={"path": str(target)}
        ) from e

    # dir 必须是目标所在目录: os.replace 的原子性只在同一文件系统内成立。
    # 若改用 mkstemp() 默认的 /tmp, 跨挂载点时 replace 退化为"复制加删除",
    # 中途崩溃会留下半截文件; 而测试在同一文件系统上跑, 永远发现不了这个退化。
    fd, tmp_name = tempfile.mkstemp(prefix=".agent_write_", dir=target.parent)
    try:
        # 写入临时文件
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            # fsync 必须在 replace 之前: replace 只保证目录项的原子切换, 不保证内容已落盘。
            # 删掉这行, 掉电后会出现"文件名已指向新内容但内容是空的"。
            os.fsync(fh.fileno())
        # 原子替换
        os.replace(tmp_name, target)
    except OSError as e:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise WorkspaceViolation(
            f"文件写入失败: {e}", code=WorkspaceErrorCode.WRITE_FAILED, detail={"path": str(target)}
        ) from e
    return WriteResult(path=target, byte_size=len(data), created=created)


_workspace_var: ContextVar[WorkspacePolicy | None] = ContextVar("workspace_policy", default=None)
"""当前上下文绑定的工作区策略。"""


def bind_workspace(policy: WorkspacePolicy) -> Token[WorkspacePolicy | None]:
    """把一份工作区策略绑定到当前上下文。

    Args:
        policy: 要绑定的策略。

    Returns:
        Token: 用于 reset_workspace 还原的令牌。
    """
    return _workspace_var.set(policy)


def reset_workspace(token: Token[WorkspacePolicy | None]) -> None:
    """还原此前绑定的工作区策略。

    Args:
        token: bind_workspace 返回的令牌。
    """
    _workspace_var.reset(token)


@contextmanager
def use_workspace(policy: WorkspacePolicy) -> Generator[WorkspacePolicy, None, None]:
    """以 with 语句绑定工作区策略, 退出时自动还原。

    Args:
        policy: 要绑定的策略。

    Yields:
        WorkspacePolicy: 传入的策略本身。
    """
    token = bind_workspace(policy)
    try:
        yield policy
    finally:
        reset_workspace(token)


@lru_cache
def default_workspace() -> WorkspacePolicy:
    """构造进程级默认工作区策略。

    Returns:
        WorkspacePolicy: 由 WorkspaceSettings 构造的默认策略。

    Raises:
        WorkspaceViolation: 未配置 WORKSPACE_ROOT, 或配置的目录不存在。
    """
    settings = get_workspacesettings()
    # 没有配置工作区根目录
    if not settings.workspace_root:
        raise WorkspaceViolation(
            message="未配置 WORKSPACE_ROOT",
            code=WorkspaceErrorCode.WORKSPACE_NOT_CONFIGURED,
            detail=None,
        )
    root = Path(settings.workspace_root)
    # 判断工作区根目录是不是有效
    if not root.is_dir():
        raise WorkspaceViolation(
            message=f"工作区根目录不是有效目录: {settings.workspace_root}",
            code=WorkspaceErrorCode.WORKSPACE_ROOT_INVALID,
            detail={"workspace_root": str(root)},
        )
    return WorkspacePolicy(
        root=root,
        max_read_bytes=settings.workspace_max_read_bytes,
        max_write_bytes=settings.workspace_max_write_bytes,
        deny_patterns=tuple(settings.workspace_deny_patterns),
    )


def current_workspace() -> WorkspacePolicy:
    """获取当前上下文生效的工作区策略。

    Returns:
        WorkspacePolicy: 已绑定的策略, 未绑定时返回进程级默认策略。

    Raises:
        WorkspaceViolation: 未绑定且默认策略不可用。
    """
    policy = _workspace_var.get()
    return policy if policy is not None else default_workspace()
