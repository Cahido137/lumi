"""测试库连通与隔离的冒烟测试"""
from sqlalchemy import text

from app.db.models import User
from app.db.session import SessionLocal


async def test_01_db_starts_empty(clean_db):
    """迁移已建表, 且库中无数据"""
    async with SessionLocal() as db:
        result = await db.scalar(text("SELECT to_regclass('public.users')"))
        assert result == "users"
        count = await db.scalar(text("SELECT count(*) FROM users"))
        assert count == 0


async def test_02_can_insert_user(clean_db):
    """能正常写入测试库"""
    async with SessionLocal() as db:
        db.add(User(username="tester", password_hash="x"))
        await db.commit()
        count = await db.scalar(text("SELECT count(*) FROM users"))
        assert count == 1


async def test_03_cleanup_between_tests(clean_db):
    """上个测试插入的数据已被清理, 隔离生效"""
    async with SessionLocal() as db:
        count = await db.scalar(text("SELECT count(*) FROM users"))
        assert count == 0
