from alembic import command
from alembic.config import Config


def main():
    command.upgrade(Config("alembic.ini"), "head")
    print("数据库迁移完成")


if __name__ == "__main__":
    main()
