"""时间工具"""

from datetime import datetime

from langchain_core.tools import tool


@tool(parse_docstring=True)
def get_current_time() -> str:
    """
    获取当前系统时间。涉及需要知道时间的任务时应调用此工具。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
