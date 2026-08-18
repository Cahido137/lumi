"""FastAPI 入口"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core import compat  # 兼容层
from app.core.checkpoint import close_checkpoint, setup_checkpoint
from app.routers import chat, sessions, ws, approvals
from app.utils.exception_handlers import register_exception_handlers


@asynccontextmanager
async def lifespan(app: FastAPI):
    """生命周期钩子"""
    await setup_checkpoint()  # 初始化检查表
    print("[INFO] 检查点已初始化")
    # 运行中
    yield
    await close_checkpoint()  # 关闭检查点
    print("[INFO] 检查点已关闭")


app = FastAPI(lifespan=lifespan)

# 注册全局异常处理器
register_exception_handlers(app)

# 跨域中间件注册
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载路由
app.include_router(chat.router)
app.include_router(sessions.router)
app.include_router(ws.router)
app.include_router(approvals.router)