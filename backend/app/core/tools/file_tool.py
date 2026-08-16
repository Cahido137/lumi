"""文件读写工具"""

from pathlib import Path

from langchain_core.tools import tool


READ_TEXT_MAX = 5000

@tool(parse_docstring=True)
async def read_file(path: str, encoding: str = "utf-8") -> str:
    """
    读取文本文件内容, 用于查看代码、配置文件、日志等。

    Args:
        path: 文件的完整路径
        encoding: 文件编码格式

    Returns:
        文件内容
    """
    try:
        p = Path(path)  # 转换路径
        if not p.is_file():
            return f"错误: 指定文件不存在 {path}"
        content = p.read_text(encoding=encoding, errors="replace")
        if len(content) > READ_TEXT_MAX:
            content = f"已截取前{READ_TEXT_MAX}字符: {content[:READ_TEXT_MAX]}"
        return content
    except Exception as e:
        return f"读取失败: {e}"


@tool(parse_docstring=True)
async def write_file(path: str, content: str, encoding: str = "utf-8") -> str:
    """
    以覆盖形式将指定内容以指定编码写入指定文件。

    Args:
        path: 文件完整路径
        content: 写入的完整文本内容
        encoding: 文件编码格式
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return f"已写入{path}, 共写入{len(content)}字符"
    except Exception as e:
        return f"写入失败: {e}"