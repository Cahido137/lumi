from app.core.tools.time_tool import get_current_time
from app.core.tools.calculator_tool import calculator
from app.core.tools.http_tool import http_get
from app.core.tools.file_tool import read_file, write_file
from app.core.tools.shell_tool import run_shell
from app.core.tools.web_search_tool import web_search


# 工具集合
TOOLS = [
    get_current_time,
    calculator,
    http_get,
    read_file,
    write_file,
    run_shell,
    web_search
]