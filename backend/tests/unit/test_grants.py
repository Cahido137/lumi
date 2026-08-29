"""grants授权判断单元测试"""

from app.core.grants import Grants, extract_grant_key

def test_tool_scope_granted():
    """工具级整体授权测试"""
    grants = Grants(tool=["run_shell"])
    assert grants.is_granted("run_shell", {"command": "ls"}) is True

def test_command_scope_granted():
    """命令级授权测试"""
    granted_key = extract_grant_key("run_shell", {"command": "ls"})
    grants = Grants(command={"run_shell": [granted_key]})
    assert grants.is_granted("run_shell", {"command": "ls"}) is True
    assert grants.is_granted("run_shell", {"command": "rm -rf /"}) is False

def test_no_grant_denined():
    """没有任何授权测试"""
    grants = Grants()
    assert grants.is_granted("write_file", {"path": "tmp/a.txt"}) is False

def test_grant_key_ignore_extra_args():
    """测试命令授权是否只取核心命令忽略无关参数"""
    key1 = extract_grant_key("run_shell", {"command": "ls", "timeout": 30})
    key2 = extract_grant_key("run_shell", {"command": "ls"})
    assert key1 == key2