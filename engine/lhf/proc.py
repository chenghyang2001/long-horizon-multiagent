"""子程序執行：逾時或中斷時殺掉「整棵程序樹」，且不會被孫程序卡住。

兩個實測過的坑（2026-11-21 HP1，`bash -c "sleep 25 & sleep 100"`）：
1. subprocess.run(timeout=...) 只殺直接子程序；Git Bash 的背景孫程序 taskkill /T 也殺不到
   （MSYS 程序的 Windows 父子關係不完整）。
   → Windows 用 Job Object：以 CREATE_SUSPENDED 建立程序、放進 job 後才恢復執行，之後所有子孫都在
     同一個 job 裡，TerminateJobObject 一次全殺；job 失敗時退回 taskkill /T。
     POSIX 用 start_new_session＋killpg。
2. 孫程序繼承了輸出管線，主程序 communicate() 會一直等到孫程序結束。
   → 輸出一律寫暫存檔，不用管線；殺完樹直接讀檔即可返回。
"""

import codecs
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

POLL_SEC = 0.2
INTERRUPT_GRACE_SEC = 60

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _NTDLL = ctypes.WinDLL("ntdll")
    _K32.CreateJobObjectW.restype = wintypes.HANDLE
    _K32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _K32.OpenProcess.restype = wintypes.HANDLE
    _K32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _K32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _K32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _K32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _K32.CloseHandle.argtypes = [wintypes.HANDLE]
    _NTDLL.NtResumeProcess.argtypes = [wintypes.HANDLE]
    PROCESS_ALL_ACCESS = 0x1F0FFF
    CREATE_SUSPENDED = 0x00000004
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_EXTENDED_LIMIT_INFO = 9

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


@dataclass
class ProcResult:
    returncode: object  # int；逾時為 None
    stdout: str
    stderr: str
    timed_out: bool


class Tree:
    """一個子程序＋它所有的子孫。"""

    def __init__(self, argv, *, cwd, env, stdin, stdout, stderr):
        flags = (
            {"start_new_session": True}
            if os.name != "nt"
            else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED}
        )
        self.job = None
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr, **flags)
        if os.name == "nt":
            self._attach_job_and_resume()

    def _attach_job_and_resume(self):
        handle = _K32.OpenProcess(PROCESS_ALL_ACCESS, False, self.proc.pid)
        try:
            job = _K32.CreateJobObjectW(None, None)
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = job and _K32.SetInformationJobObject(
                job, JOB_EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info)
            )
            if ok and _K32.AssignProcessToJobObject(job, handle):
                self.job = job
            elif job:
                _K32.CloseHandle(job)
        finally:
            _NTDLL.NtResumeProcess(handle)  # 不論 job 成功與否都一定要恢復，否則子程序永遠停住
            _K32.CloseHandle(handle)

    def kill(self):
        """殺整棵樹；已結束的錯誤一律忽略。"""
        try:
            if self.job:
                _K32.TerminateJobObject(self.job, 1)
            elif os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    check=False,
                    timeout=30,
                )
            else:
                os.killpg(self.proc.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            self.proc.kill()
        except OSError:
            pass

    def interrupt(self):
        """請子程序自己收尾（存檔）：Windows 送 CTRL_BREAK、POSIX 對整組送 SIGINT。"""
        try:
            if os.name == "nt":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(self.proc.pid, signal.SIGINT)
        except (OSError, ValueError):
            pass

    def close(self):
        if self.job:
            _K32.CloseHandle(self.job)  # KILL_ON_JOB_CLOSE：關掉 handle 時殘留的子孫也一起結束
            self.job = None


def read_back(handle):
    handle.seek(0)
    return handle.read().decode("utf-8", "replace")


def write_stdin(proc, input_text):
    if input_text is None:
        return
    try:
        proc.stdin.write(input_text.encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass  # 子程序沒讀完就結束；結果以結束碼與輸出為準


def wait_for(proc, timeout):
    """回傳 True＝在時限內結束。用輪詢而不是 wait(timeout)，Ctrl+C 才能即時打斷。"""
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SEC)
    return True


def run_tree(argv, *, cwd, env, timeout, input_text=None):
    """執行並收集輸出；逾時殺整棵樹。KeyboardInterrupt 時也先殺樹再往上丟。"""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        stdin = subprocess.PIPE if input_text is not None else subprocess.DEVNULL
        tree = Tree(argv, cwd=cwd, env=env, stdin=stdin, stdout=out, stderr=err)
        try:
            write_stdin(tree.proc, input_text)
            finished = wait_for(tree.proc, timeout)
            if not finished:
                tree.kill()
                wait_for(tree.proc, 10)
            return ProcResult(tree.proc.returncode if finished else None, read_back(out), read_back(err), not finished)
        except KeyboardInterrupt:
            tree.kill()
            raise
        finally:
            tree.close()


class Streamer:
    """把子程序寫進檔案的輸出增量讀出（UTF-8 多位元組被切在中間也能正確解碼）。"""

    def __init__(self, handle):
        self.handle = handle
        self.position = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read_new(self, final=False):
        self.handle.seek(self.position)
        data = self.handle.read()
        self.position += len(data)
        return self.decoder.decode(data, final=final)


def stream_tree(argv, *, cwd, env, timeout, on_output, on_tick=None):
    """執行並即時轉印輸出。回傳 (結束碼或 None, 狀態)；狀態：ok／timeout／interrupted。"""
    with tempfile.TemporaryFile() as out:
        tree = Tree(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        streamer = Streamer(out)
        deadline = time.monotonic() + timeout
        try:
            return _stream_loop(tree, streamer, deadline, on_output)
        except KeyboardInterrupt:
            return _graceful_stop(tree, streamer, on_output)
        finally:
            on_output(streamer.read_new(final=True))
            tree.close()


def _stream_loop(tree, streamer, deadline, on_output):
    while tree.proc.poll() is None:
        on_output(streamer.read_new())
        if time.monotonic() >= deadline:
            tree.kill()
            wait_for(tree.proc, 10)
            return None, "timeout"
        time.sleep(POLL_SEC)
    return tree.proc.returncode, "ok"


def _graceful_stop(tree, streamer, on_output):
    """Ctrl+C：先請子程序存檔收尾，等不到才殺樹；收尾中再按一次 Ctrl+C 就直接殺。"""
    tree.interrupt()
    try:
        finished = wait_for(tree.proc, INTERRUPT_GRACE_SEC)
    except KeyboardInterrupt:
        finished = False
    if not finished:
        tree.kill()
        wait_for(tree.proc, 10)
    on_output(streamer.read_new())
    return tree.proc.returncode, "interrupted"
