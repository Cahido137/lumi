"""受限工作区策略、子进程环境白名单与文件/命令工具接线的单元测试"""

import os

import app.core.execution.workspace as ws
import pytest
from app.config import get_workspacesettings
from app.core.execution import scrubbed_env
from app.core.tools.file_tool import read_file, write_file
from app.core.tools.shell_tool import run_shell
from app.utils.errors import WorkspaceViolation

DENY = (".env", ".env.*", ".git", ".git/*", "*.pem")


@pytest.fixture()
def policy(tmp_path):
    """以 pytest 临时目录为根、限额收紧到 1KiB 的工作区策略"""
    return ws.WorkspacePolicy(root=tmp_path, max_read_bytes=1024, max_write_bytes=1024, deny_patterns=DENY)


@pytest.fixture()
def clear_cache():
    """清理配置与默认策略缓存, 避免用例间互相污染"""
    get_workspacesettings.cache_clear()
    ws.default_workspace.cache_clear()
    yield
    get_workspacesettings.cache_clear()
    ws.default_workspace.cache_clear()


def _code(fn):
    """执行 fn 并返回其抛出的 WorkspaceViolation 错误码"""
    with pytest.raises(WorkspaceViolation) as ei:
        fn()
    return ei.value.error_code


async def _acode(fn):
    """await fn() 并返回其抛出的 WorkspaceViolation 错误码"""
    with pytest.raises(WorkspaceViolation) as ei:
        await fn()
    return ei.value.error_code


# ── 路径归属 ────────────────────────────────────────────────────────────────


def test_relative_and_absolute_inside_allowed(policy, tmp_path):
    """工作区内的相对路径与绝对路径都应解析成功"""
    expected = (tmp_path / "src" / "a.py").resolve()
    assert ws.resolve_within_workspace(policy, "src/a.py") == expected
    assert ws.resolve_within_workspace(policy, str(tmp_path / "src" / "a.py")) == expected


def test_parent_traversal_rejected(policy):
    """含 .. 的逃逸路径必须被拒绝"""
    assert _code(lambda: ws.resolve_within_workspace(policy, "../../etc/passwd")) == "path_outside_workspace"


def test_outside_absolute_rejected(policy):
    """工作区外的绝对路径必须被拒绝"""
    assert _code(lambda: ws.resolve_within_workspace(policy, "/etc/passwd")) == "path_outside_workspace"


def test_symlink_escape_rejected(policy, tmp_path):
    """指向工作区外的符号链接在 resolve 后必须被识别为越界"""
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link_out"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不支持符号链接")
    assert _code(lambda: ws.resolve_within_workspace(policy, "link_out", must_exist=True)) == "path_outside_workspace"


@pytest.mark.parametrize("raw", [".env", "deploy/.env", ".git/config", "keys/server.pem"])
def test_deny_patterns_block_at_any_depth(policy, raw):
    """拒绝模式应能拦住任意层级下的敏感文件"""
    assert _code(lambda: ws.resolve_within_workspace(policy, raw)) == "path_denied"


def test_root_resolvable_but_not_readable(policy, tmp_path):
    """根目录可解析(列目录需要), 但按文件读取会得到 not_a_file"""
    assert ws.resolve_within_workspace(policy, ".") == tmp_path.resolve()
    assert _code(lambda: ws.resolve_within_workspace(policy, ".", must_exist=True)) == "not_a_file"


def test_empty_path_rejected(policy):
    """空与纯空白路径都应被拒绝"""
    for raw in ("", "   "):
        assert _code(lambda raw=raw: ws.resolve_within_workspace(policy, raw)) == "empty_path"


def test_missing_file_and_directory_distinguished(policy, tmp_path):
    """不存在报 not_found, 存在但是目录报 not_a_file"""
    (tmp_path / "sub").mkdir()
    assert _code(lambda: ws.resolve_within_workspace(policy, "nope.txt", must_exist=True)) == "not_found"
    assert _code(lambda: ws.resolve_within_workspace(policy, "sub", must_exist=True)) == "not_a_file"


# ── 读写限额 ────────────────────────────────────────────────────────────────


def test_read_byte_truncation(policy, tmp_path):
    """超出字节上限时只读入上限字节并标记截断"""
    (tmp_path / "big.txt").write_bytes(b"x" * 5000)
    r = ws.read_text_bounded(tmp_path / "big.txt", max_bytes=1024, max_chars=None)
    assert (r.byte_size, r.truncated_bytes, len(r.text)) == (1024, True, 1024)


def test_read_char_truncation_independent(policy, tmp_path):
    """字符上限与字节上限独立生效"""
    (tmp_path / "cn.txt").write_text("中" * 200, encoding="utf-8")
    r = ws.read_text_bounded(tmp_path / "cn.txt", max_bytes=1024, max_chars=50)
    assert r.truncated_chars and not r.truncated_bytes and len(r.text) == 50


def test_read_exact_limit_not_truncated(policy, tmp_path):
    """文件大小恰好等于上限时不应误报截断"""
    (tmp_path / "exact.txt").write_bytes(b"y" * 1024)
    assert ws.read_text_bounded(tmp_path / "exact.txt", max_bytes=1024, max_chars=None).truncated_bytes is False


def test_read_bad_encoding_and_dir(policy, tmp_path):
    """编码名非法与目标是目录都应转为 WorkspaceViolation"""
    (tmp_path / "cn.txt").write_text("中", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    assert _code(lambda: ws.read_text_bounded(tmp_path / "cn.txt", encoding="nope", max_bytes=64)) == "bad_encoding"
    assert _code(lambda: ws.read_text_bounded(tmp_path / "sub", max_bytes=64)) == "read_failed"


def test_write_atomic_created_flag_and_no_residue(policy, tmp_path):
    """写入应区分新建与覆盖, 且不残留临时文件"""
    target = tmp_path / "sub" / "out.txt"
    assert ws.write_text_atomic(target, "hello", max_bytes=1024).created is True
    assert ws.write_text_atomic(target, "world", max_bytes=1024).created is False
    assert target.read_text(encoding="utf-8") == "world"
    assert not [p for p in (tmp_path / "sub").iterdir() if p.name.startswith(".agent_write_")]


def test_write_over_limit_zero_side_effect(policy, tmp_path):
    """超出写入上限时拒绝, 且不创建目标文件"""
    target = tmp_path / "over.txt"
    assert _code(lambda: ws.write_text_atomic(target, "y" * 2048, max_bytes=1024)) == "payload_too_large"
    assert not target.exists()


def test_write_dir_is_directory(policy, tmp_path):
    """写入目录应在任何磁盘操作前被拒绝"""
    (tmp_path / "sub").mkdir()
    before = {p.name for p in tmp_path.iterdir()}
    assert _code(lambda: ws.write_text_atomic(tmp_path / "sub", "x", max_bytes=1024)) == "is_directory"
    assert {p.name for p in tmp_path.iterdir()} == before


def test_write_dangling_symlink_treated_as_new(policy, tmp_path):
    """指向不存在文件的坏符号链接按新建处理"""
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "not-there")
    assert ws.write_text_atomic(link, "now real", max_bytes=1024).created is True
    assert link.read_text(encoding="utf-8") == "now real"


def test_write_byte_size_is_bytes(policy, tmp_path):
    """byte_size 是字节数而不是字符数"""
    r = ws.write_text_atomic(tmp_path / "u.txt", "中文内容", max_bytes=1024)
    assert r.byte_size == 12


# ── 上下文绑定 ──────────────────────────────────────────────────────────────


def test_bind_and_reset(policy):
    """bind 后 current_workspace 返回绑定值, reset 后还原"""
    token = ws.bind_workspace(policy)
    try:
        assert ws.current_workspace() is policy
    finally:
        ws.reset_workspace(token)
    assert ws._workspace_var.get() is None


def test_use_workspace_restores_on_error(policy):
    """with 体内抛异常也必须还原绑定"""
    with pytest.raises(RuntimeError):
        with ws.use_workspace(policy):
            raise RuntimeError("boom")
    assert ws._workspace_var.get() is None


def test_nested_binding_unwinds_in_order(policy, tmp_path):
    """嵌套绑定应逐层还原"""
    other = ws.WorkspacePolicy(root=tmp_path)
    with ws.use_workspace(policy):
        with ws.use_workspace(other):
            assert ws.current_workspace() is other
        assert ws.current_workspace() is policy
    assert ws._workspace_var.get() is None


async def test_binding_visible_in_thread_pool(policy, tmp_path):
    """下放线程池的调用同样能读到绑定值"""
    import asyncio

    with ws.use_workspace(policy):
        assert await asyncio.to_thread(ws.resolve_within_workspace, policy, "a.txt") == (tmp_path / "a.txt").resolve()


# ── 默认策略与失败关闭 ──────────────────────────────────────────────────────


def test_fail_closed_when_unconfigured(monkeypatch, clear_cache):
    """未配置 WORKSPACE_ROOT 时必须失败关闭"""
    monkeypatch.setenv("WORKSPACE_ROOT", "")
    assert _code(ws.current_workspace) == "workspace_not_configured"


def test_invalid_root_fail_closed(monkeypatch, tmp_path, clear_cache):
    """WORKSPACE_ROOT 指向不存在的目录时同样失败关闭"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "not-exist"))
    assert _code(ws.current_workspace) == "workspace_root_invalid"


def test_default_workspace_reads_settings(monkeypatch, tmp_path, clear_cache):
    """配置有效时回落策略应读取配置值"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKSPACE_MAX_READ_BYTES", "2048")
    p = ws.current_workspace()
    assert p.root == tmp_path.resolve()
    assert p.max_read_bytes == 2048


def test_failure_not_cached(monkeypatch, tmp_path, clear_cache):
    """失败关闭的异常不应被 lru_cache 缓存"""
    monkeypatch.setenv("WORKSPACE_ROOT", "")
    assert _code(ws.default_workspace) == "workspace_not_configured"
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    get_workspacesettings.cache_clear()
    assert ws.default_workspace().root == tmp_path.resolve()


def test_list_settings_accept_comma_separated_env(monkeypatch, clear_cache):
    """列表型配置支持逗号分隔写法, 不会在启动时抛 SettingsError"""
    monkeypatch.setenv("WORKSPACE_DENY_PATTERNS", ".env, secrets/* ,")
    monkeypatch.setenv("WORKSPACE_ENV_ALLOW", "NO_COLOR,JAVA_HOME")
    s = get_workspacesettings()
    assert s.workspace_deny_patterns == [".env", "secrets/*"]
    assert s.workspace_env_allow == ["NO_COLOR", "JAVA_HOME"]


# ── 子进程环境白名单 ────────────────────────────────────────────────────────


def test_scrubbed_env_drops_own_secrets(monkeypatch):
    """本项目配置项一律不透传"""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LLM_API_KEY", "sk-leak")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@db/x")
    monkeypatch.setenv("JWT_SECRET", "jwt-leak")
    env = scrubbed_env()
    assert env["PATH"] == "/usr/bin"
    for k in ("LLM_API_KEY", "DATABASE_URL", "JWT_SECRET"):
        assert k not in env


def test_scrubbed_env_allow_list_and_fragment_override(monkeypatch, clear_cache):
    """放行列表生效, 但带凭据语义的名字即使显式放行也被拦下"""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-leak")
    monkeypatch.setenv("WORKSPACE_ENV_ALLOW", "NO_COLOR,AWS_SECRET_ACCESS_KEY")
    env = scrubbed_env()
    assert env.get("NO_COLOR") == "1"
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_scrubbed_env_extra_and_empty(monkeypatch):
    """extra 注入生效且不污染父进程, 空值变量被丢弃"""
    monkeypatch.setenv("LANG", "")
    env = scrubbed_env({"LUMI_RUN_ID": "run-1"})
    assert env["LUMI_RUN_ID"] == "run-1"
    assert "LANG" not in env
    assert os.environ.get("LUMI_RUN_ID") is None


# ── 工具接线 ────────────────────────────────────────────────────────────────


async def test_read_file_tool(policy, tmp_path):
    """read_file 走策略解析并带截断说明"""
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "cn.txt").write_text("中" * 400, encoding="utf-8")
    with ws.use_workspace(policy):
        assert await read_file.ainvoke({"path": "a.txt"}) == "hello"
        out = await read_file.ainvoke({"path": "cn.txt"})
    assert out.startswith("[内容已截断, 上限1024字节")


async def test_read_file_tool_rejects_escape_and_deny(policy, tmp_path):
    """read_file 对越界与拒绝模式抛 WorkspaceViolation"""
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    with ws.use_workspace(policy):
        assert await _acode(lambda: read_file.ainvoke({"path": "../../etc/passwd"})) == "path_outside_workspace"
        assert await _acode(lambda: read_file.ainvoke({"path": ".env"})) == "path_denied"


async def test_write_file_tool_returns_relative_path(policy, tmp_path):
    """write_file 回传相对路径、字节数与新建/覆盖标记"""
    with ws.use_workspace(policy):
        a = await write_file.ainvoke({"path": "out/b.txt", "content": "中文"})
        b = await write_file.ainvoke({"path": "out/b.txt", "content": "x"})
    assert a == "已新建out/b.txt, 共写入6字节"
    assert b == "已覆盖out/b.txt, 共写入1字节"
    assert str(tmp_path) not in a


async def test_tools_fail_closed_when_unbound(monkeypatch, clear_cache):
    """未绑定且未配置时工具拒绝执行"""
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    assert await _acode(lambda: read_file.ainvoke({"path": "a.txt"})) == "workspace_not_configured"


@pytest.mark.skipif(os.name == "nt", reason="pwd 与 env 是 POSIX 命令")
async def test_run_shell_cwd_and_scrubbed_env(policy, tmp_path, monkeypatch, clear_cache):
    """run_shell 的工作目录为工作区根, 且环境中不含密钥"""
    monkeypatch.setenv("LLM_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-should-not-leak")
    with ws.use_workspace(policy):
        out = await run_shell.ainvoke({"command": "pwd && env"})
    assert str(policy.root) in out
    assert "sk-should-not-leak" not in out
    assert "aws-should-not-leak" not in out


async def test_run_shell_fail_closed(monkeypatch, clear_cache):
    """未配置工作区时 run_shell 不启动任何子进程"""
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    assert await _acode(lambda: run_shell.ainvoke({"command": "pwd"})) == "workspace_not_configured"
