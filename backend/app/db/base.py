"""ORM 声明式基类定义。"""

from sqlalchemy.orm import DeclarativeBase


# ORM 模型基类
class Base(DeclarativeBase):
    """全部 ORM 模型的声明式基类。

    Note:
        所有表模型应继承本类。Alembic 的自动迁移会以此类 Base.metadata 为目标元数据。
    """
