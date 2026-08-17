"""FastAPI 入口"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import chat, sessions, ws
from app.utils.exception_handlers import register_exception_handlers


@asynccontextmanager
async def lifespan(app: FastAPI):
    """生命周期钩子"""
    # 运行前
    print("[INFO] Start")
    # 运行中
    yield
    # 运行后
    print("[INFO] END")


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