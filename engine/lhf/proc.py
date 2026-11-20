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

具名引擎 Job：持鎖的引擎程序另外建立一個具名 job（名稱記在 run.lock），每個子程序先放進它、再放進
各自的逾時 job（巢狀）。之後子孫不論怎麼脫離父子關係（start /b、Start-Process、孤兒）都還在這個 job 裡，
人工指令用 IsProcessInJob 判斷「我是不是引擎底下的程序」。名稱外洩沒關係：知道名稱也無法脫離 job。
POSIX 沒有對應機制，這層檢查略過（仍有環境旗標與祖先鏈兩層）。
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
    _K32.OpenJobObjectW.restype = wintypes.HANDLE
    _K32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _K32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    _K32.GetCurrentProcess.restype = wintypes.HANDLE
    JOB_OBJECT_QUERY = 0x0004
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


_ENGINE_JOB = {"name": None, "handle": None, "failed": False}


def set_engine_job(name):
    """持鎖的引擎程序呼叫一次；之後這個程序啟動的子程序都會放進這個具名 job。"""
    _ENGINE_JOB.update(name=name, handle=None, failed=False)


def engine_job_handle():
    """第一次用到時才建立；建立失敗只警告一次，不擋執行（仍有旗標與祖先鏈兩層）。"""
    if os.name != "nt" or not _ENGINE_JOB["name"] or _ENGINE_JOB["failed"]:
        return None
    if _ENGINE_JOB["handle"] is None:
        try:
            _ENGINE_JOB["handle"] = _new_job(_ENGINE_JOB["name"])
        except OSError as exc:
            _ENGINE_JOB["failed"] = True
            print(f"（警告：無法建立具名引擎 Job（{exc}），人工指令的 Job 檢查停用）", file=sys.stderr, flush=True)
    return _ENGINE_JOB["handle"]


def in_named_job(name):
    """目前程序是否在名為 name 的 job 裡；job 不存在或非 Windows 一律 False。"""
    if os.name != "nt" or not name:
        return False
    job = _K32.OpenJobObjectW(JOB_OBJECT_QUERY, False, name)
    if not job:
        return False
    try:
        result = wintypes.BOOL(False)
        ok = _K32.IsProcessInJob(_K32.GetCurrentProcess(), job, ctypes.byref(result))
        return bool(ok and result.value)
    finally:
        _K32.CloseHandle(job)


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
        engine_job = engine_job_handle()
        if engine_job and not _K32.AssignProcessToJobObject(engine_job, handle):
            print(
                f"（警告：子程序無法放進具名引擎 Job（錯誤碼 {ctypes.get_last_error()}））", file=sys.stderr, flush=True
            )
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


def _new_job(name=None):
    """建立「關掉 handle 就殺光所有成員」的 job；失敗丟 OSError。"""
    job = _K32.CreateJobObjectW(None, name)
    if not job:
        raise OSError(f"CreateJobObject 失敗（錯誤碼 {ctypes.get_last_error()}）")
    if name and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS：名稱被占用，不能信任這個 job
        _K32.CloseHandle(job)
        raise OSError(f"Job 名稱 {name} 已存在")
    info = _ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _K32.SetInformationJobObject(job, JOB_EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        _K32.CloseHandle(job)
        raise OSError(f"設定 Job 失敗（錯誤碼 {error}）")
    return job


def _create_job(handle):
    """每棵程序樹自己的逾時 job；程序已在引擎 job 裡時，這個 job 會巢狀在它底下。"""
    job = _new_job()
    if not _K32.AssignProcessToJobObject(job, handle):
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
    """執行並即時轉印輸出。回傳 (結束碼或 None, 狀態)；狀態：ok／timeout／interrupted／stopped。
    on_tick：每次輪詢呼叫一次，回傳 True＝請子程序收尾停止（stop --now；送 CTRL_BREAK，與 Ctrl+C 同樣流程）。"""
    with tempfile.TemporaryFile() as out:
        tree = Tree(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        streamer = Streamer(out)
        deadline = time.monotonic() + timeout
        try:
            return _stream_loop(tree, streamer, deadline, on_output, on_tick)
        except KeyboardInterrupt:
            return _graceful_stop(tree, streamer, on_output)
        finally:
            on_output(streamer.read_new(final=True))
            tree.close()


def _stream_loop(tree, streamer, deadline, on_output, on_tick=None):
    while tree.proc.poll() is None:
        on_output(streamer.read_new())
        if time.monotonic() >= deadline:
            tree.kill()
            wait_for(tree.proc, 10)
            return None, "timeout"
        if on_tick is not None and on_tick():
            return _graceful_stop(tree, streamer, on_output, status="stopped")
        time.sleep(POLL_SEC)
    return tree.proc.returncode, "ok"


def _graceful_stop(tree, streamer, on_output, status="interrupted"):
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
    return tree.proc.returncode, status
