"""e2e 目录的测试自动获得测试库环境与数据清理"""

import pytest


@pytest.fixture(autouse=True)
def _e2e_env(test_db, checkpoint, clean_db):
    pass
