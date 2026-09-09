"""平台兼容层。

Note:
    导入本模块即把 Windows 上的全局事件循环策略切换为 SelectorEventLoop。
"""

import asyncio
import sys

# Windows 平台将全局切换到 SelectorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def selector_loop_factory(use_subprocess: bool = False):
    """创建 SelectorEventLoop, 可作为事件循环工厂使用。

    Args:
        use_subprocess: 保留参数。uvicorn 0.36 起以零参数调用循环工厂, 此参数不会传入。

    Returns:
        asyncio.SelectorEventLoop: 新建的选择器事件循环。

    Note:
        本函数会以导入字符串的形式传给 uvicorn 的 loop 参数, 不应该在其他地方被直接调用。
    """
    return asyncio.SelectorEventLoop()
