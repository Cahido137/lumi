import asyncio

from app.db import models  # noqa: F401  导入即注册模型到 Base.metadata
from app.db.base import Base
from app.db.session import async_engine


async def main():
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("所有表已创建")


asyncio.run(main())