"""Agent 可用工具的集合与导出。"""

from app.core.tools.calculator_tool import calculator
from app.core.tools.file_tool import read_file, write_file
from app.core.tools.http_tool import http_get
from app.core.tools.shell_tool import run_shell
from app.core.tools.time_tool import get_current_time
from app.core.tools.todo_tool import mark_todo_done, mark_todo_start
from app.core.tools.web_search_tool import web_search

TOOLS = [
    get_current_time,
    calculator,
    http_get,
    read_file,
    write_file,
    run_shell,
    web_search,
    mark_todo_done,
    mark_todo_start,
]
"""暴露给模型的全部工具列表, 模型只绑定此处声明的工具。"""
