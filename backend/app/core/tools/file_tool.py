"""文件读写工具。

Note:
    路径一律先经受限工作区策略解析, 越界、命中拒绝规则或超出字节上限时抛 WorkspaceViolation,
    由执行层统一转换为 status="error" 的工具消息回传给模型。
    磁盘 IO 通过 asyncio.to_thread 下放线程池, 避免大文件或慢盘阻塞事件循环。
"""

import asyncio

from langchain_core.tools import tool

from app.core.execution.workspace import (
    ReadResult,
    WorkspacePolicy,
    current_workspace,
    read_text_bounded,
    resolve_within_workspace,
    write_text_atomic,
)

READ_TEXT_MAX = 5000
"""回传给模型的最大字符数, 与策略中的字节上限独立生效。"""


def _truncate_note(result: ReadResult, policy: WorkspacePolicy) -> str:
    """构造读取结果的截断说明前缀。

    Args:
        result: 受限读取的结果。
        policy: 生效的工作区策略。

    Returns:
        str: 未截断时为空串, 否则为一行说明。
    """
    if not (result.truncated_bytes or result.truncated_chars):
        return ""
    # 字节上限意味着磁盘上还有内容没读, 字符上限意味着读全了但回传被裁, 模型对两者的后续动作不同
    limit = f"{policy.max_read_bytes}字节" if result.truncated_bytes else f"{READ_TEXT_MAX}字符"
    return f"[内容已截断, 上限{limit}, 实际读入{result.byte_size}字节]\n"


@tool(parse_docstring=True)
async def read_file(path: str, encoding: str = "utf-8") -> str:
    """
    读取受限工作区内文本文件的内容, 用于查看代码、配置文件、日志等。

    Args:
        path: 相对工作区根目录的路径, 或工作区内的绝对路径
        encoding: 文件编码格式

    Returns:
        文件内容, 超出上限时返回截断后的前缀并附带说明
    """
    policy = current_workspace()
    target = await asyncio.to_thread(resolve_within_workspace, policy, path, must_exist=True)
    result = await asyncio.to_thread(
        read_text_bounded,
        target,
        encoding=encoding,
        max_bytes=policy.max_read_bytes,
        max_chars=READ_TEXT_MAX,
    )
    return f"{_truncate_note(result, policy)}{result.text}"


@tool(parse_docstring=True)
async def write_file(path: str, content: str, encoding: str = "utf-8") -> str:
    """
    以覆盖形式将指定内容写入受限工作区内的文件, 父目录不存在时自动创建。

    Args:
        path: 相对工作区根目录的路径, 或工作区内的绝对路径
        content: 写入的完整文本内容
        encoding: 文件编码格式

    Returns:
        写入结果说明, 含工作区相对路径、字节数以及是新建还是覆盖
    """
    policy = current_workspace()
    target = await asyncio.to_thread(resolve_within_workspace, policy, path)
    result = await asyncio.to_thread(
        write_text_atomic,
        target,
        content,
        encoding=encoding,
        max_bytes=policy.max_write_bytes,
    )
    action = "已新建" if result.created else "已覆盖"
    # 回传相对路径, 不把服务器的目录结构暴露进模型上下文
    return f"{action}{policy.relative_posix(result.path)}, 共写入{result.byte_size}字节"
