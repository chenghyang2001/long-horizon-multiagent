"""子程序執行：逾時或中斷時殺掉「整棵程序樹」，且不會被孫程序卡住。

兩個實測過的坑（2026-11-21 HP1，`bash -c "sleep 25 & sleep 100"`）：
1. subprocess.run(timeout=...) 只殺直接子程序；Git Bash 的背景孫程序 taskkill /T 也殺不到
   （MSYS 程序的 Windows 父子關係不完整）。
   → Windows 用 Job Object：以 CREATE_SUSPENDED 建立程序、放進 job 後才恢復執行，之後所有子孫都在
     同一個 job 裡，TerminateJobObject 一次全殺；job 失敗時退回 taskkill /T。
     POSIX 用 start_new_session＋killpg。
2. 孫程序繼承了輸出管線，主程序 communicate() 會一直等到孫程序結束。
   → 輸出一律寫暫存檔，不用管線；殺完樹直接讀檔即可返回。
3. 輸入（prompt）用管線同步寫入時，子程序不讀就會卡在寫入、逾時根本還沒開始計算。
   → 輸入也先寫暫存檔，以檔案 handle 當 stdin；逾時從程序啟動那一刻算。
"""

import codecs
import os
import signal
import subprocess
import sys
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
        """用 Popen 自己的 process handle（不另外 OpenProcess，免得權限不足拿不到 handle、子程序永遠停在暫停）。
        job 掛不上時印警告並改用 taskkill /T 後備；恢復失敗就直接殺掉並報錯，絕不留下停住的程序。"""
        handle = int(self.proc._handle)  # noqa: SLF001 — Popen 在 Windows 上一定有這個 handle
        try:
            self.job = _create_job(handle)
        except OSError as exc:
            print(
                f"（警告：Job Object 無法使用（{exc}），逾時改用 taskkill /T；Git Bash 的背景程序可能殺不乾淨）",
                file=sys.stderr,
                flush=True,
            )
        status = _NTDLL.NtResumeProcess(handle)
        if status != 0:
            self.kill()
            raise OSError(f"子程序以暫停狀態建立後無法恢復執行（NTSTATUS 0x{status & 0xFFFFFFFF:08X}），已終止")

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


def _create_job(handle):
    """建立「關掉 handle 就殺光所有成員」的 job 並把程序放進去；任何一步失敗都丟 OSError。"""
    job = _K32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(f"CreateJobObject 失敗（錯誤碼 {ctypes.get_last_error()}）")
    info = _ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _K32.SetInformationJobObject(job, JOB_EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info))
    if not (ok and _K32.AssignProcessToJobObject(job, handle)):
        error = ctypes.get_last_error()
        _K32.CloseHandle(job)
        raise OSError(f"無法把程序放進 Job Object（錯誤碼 {error}）")
    return job


def read_back(handle):
    handle.seek(0)
    return handle.read().decode("utf-8", "replace")


def stdin_file(input_text):
    """把輸入寫進暫存檔再倒回開頭；子程序讀不讀都不會卡住主程序。沒有輸入就回 None（用 DEVNULL）。"""
    if input_text is None:
        return None
    handle = tempfile.TemporaryFile()
    handle.write(input_text.encode("utf-8"))
    handle.flush()
    handle.seek(0)
    return handle


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
    source = stdin_file(input_text)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            tree = Tree(argv, cwd=cwd, env=env, stdin=source or subprocess.DEVNULL, stdout=out, stderr=err)
        finally:
            if source:
                source.close()  # 子程序已繼承自己的 handle，這邊關掉不影響它讀
        try:
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
