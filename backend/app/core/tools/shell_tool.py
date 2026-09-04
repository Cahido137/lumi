"""Shell 命令执行工具"""

import locale
import os
import signal
import subprocess
import tempfile
import time
from typing import IO

from langchain_core.tools import tool

OUTPUT_MAX_LEN = 5000
DEFAULT_TIMEOUT = 30

_WINDOWS = os.name == "nt"

if _WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001


def _truncate(text: str) -> str:
    if len(text) > OUTPUT_MAX_LEN:
        return f"{text[:OUTPUT_MAX_LEN]}\n...(输出过长, 已截取前{OUTPUT_MAX_LEN}字符, 共{len(text)}字符)"
    return text


def _decode(data: bytes) -> str:
    """解码输出, 优先系统本地编码(如中文 Windows 的 GBK)"""
    encodings = [locale.getpreferredencoding(False), "utf-8", "gbk"]
    for enc in encodings:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _create_kill_job():
    """创建作业对象, 关闭其句柄时自动终止其下全部进程"""
    if not _WINDOWS:
        return None
    try:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _assign_to_job(job, pid) -> None:
    """将进程加入作业对象"""
    try:
        handle = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not handle:
            return
        try:
            _kernel32.AssignProcessToJobObject(job, handle)
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:
        pass


def _close_job(job) -> None:
    """关闭作业句柄, 终止其下全部进程"""
    if job:
        try:
            _kernel32.CloseHandle(job)
        except Exception:
            pass


def _unlink_quietly(path: str) -> None:
    """删除临时文件, 失败时短暂重试"""
    for _ in range(5):
        try:
            os.unlink(path)
            return
        except OSError:
            time.sleep(0.1)


def _stop_process(proc: subprocess.Popen, job) -> None:
    """强制终止进程及其子进程"""
    if proc.poll() is not None:
        return
    if _WINDOWS:
        _close_job(job)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


@tool(parse_docstring=True)
def run_shell(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """
    在系统 shell 中执行一条命令, 用于查看系统信息、运行脚本、管理进程等任务。
    命令语法遵循系统自带 shell(Windows 为 cmd, POSIX 为 sh), 不支持交互式输入。

    Args:
        command: 要执行的 shell 命令
        timeout: 命令超时时间(秒), 超时后强制终止进程

    Returns:
        命令退出码、标准输出和标准错误
    """
    out_fd, out_path = tempfile.mkstemp(prefix="shell_stdout_", suffix=".txt")
    os.close(out_fd)
    err_fd, err_path = tempfile.mkstemp(prefix="shell_stderr_", suffix=".txt")
    os.close(err_fd)
    job = _create_kill_job()
    opened = []
    try:
        out_file: IO[bytes] = open(out_path, "wb")
        err_file: IO[bytes] = open(err_path, "wb")
        opened.extend([out_file, err_file])
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=out_file,
            stderr=err_file,
            start_new_session=not _WINDOWS,
        )
        out_file.close()
        err_file.close()
        opened.clear()
        if job:
            _assign_to_job(job, proc.pid)

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_process(proc, job)
            job = None

        with open(out_path, "rb") as f:
            out = _decode(f.read())
        with open(err_path, "rb") as f:
            err = _decode(f.read())

        parts = []
        if timed_out:
            parts.append(f"执行超时: 命令超过{timeout}秒未结束, 已强制终止")
        parts.append(f"退出码: {proc.returncode}")
        if out.strip():
            parts.append(f"标准输出:\n{_truncate(out)}")
        if err.strip():
            parts.append(f"标准错误:\n{_truncate(err)}")
        if not out.strip() and not err.strip():
            parts.append("(无输出)")
        text = "\n\n".join(parts)
        # M1 大改: 超时视为工具失败, 抛异常(由执行层统一转为 status="error" 的
        # 工具消息), 并把已捕获的部分输出带进错误消息供模型参考
        if timed_out:
            raise TimeoutError(text)
        return text
    except TimeoutError:
        raise
    except Exception as e:
        # M1 大改: 启动/IO 失败通过异常传播, 不再返回错误字符串
        raise RuntimeError(f"执行失败: {e}") from e
    finally:
        for fh in opened:
            try:
                fh.close()
            except Exception:
                pass
        _close_job(job)
        for path in (out_path, err_path):
            _unlink_quietly(path)
