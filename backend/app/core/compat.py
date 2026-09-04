"""平台兼容层"""

import asyncio
import sys

# Windows 平台将全局切换到 SelectorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def selector_loop_factory(use_subprocess: bool = False):
    return asyncio.SelectorEventLoop()
