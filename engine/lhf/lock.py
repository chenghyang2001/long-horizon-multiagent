"""執行鎖 run/.runtime/run.lock：同一個專案同時只能有一個 run／hours 在改 state.json。

鎖內容＝PID＋該程序的啟動時間＋token。只看 PID 不夠：程序當掉後 PID 可能被別的程序重用，
所以要同時比對啟動時間，不符就視為殘留舊鎖、自動清掉。
hours 拿到鎖後把 token 放進環境變數 LH_LOCK_TOKEN，它啟動的每一輪 run 子程序憑 token 沿用同一把鎖。
"""

import json
import os
import secrets
import sys
from contextlib import contextmanager

from lhf.common import now_str
from lhf.errors import RoundError

LOCK_NAME = "run.lock"
TOKEN_ENV = "LH_LOCK_TOKEN"


class LockBusy(RoundError):
    """另一個 run／hours 正在執行。"""


def lock_path(cfg):
    return cfg.run_dir / ".runtime" / LOCK_NAME


def process_start_time(pid):
    """回傳程序啟動時間（同一程序永遠相同的數字）；程序不存在回 None。"""
    if os.name == "nt":
        return _windows_start_time(pid)
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return int(handle.read().rsplit(")", 1)[1].split()[19])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError):
        try:
            os.kill(pid, 0)  # 沒有 /proc 的系統（macOS）只能確認存活，啟動時間用 0 代表「不比對」
            return 0
        except OSError:
            return None


def _windows_start_time(pid):
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        exit_code = wintypes.DWORD()
        if k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value != 259:  # 259＝STILL_ACTIVE
            return None
        if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        k32.CloseHandle(handle)


def read_lock(cfg):
    try:
        data = json.loads(lock_path(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def lock_alive(info):
    """鎖的主人還活著（PID 存在且啟動時間相符）才算有效。"""
    if not info or not isinstance(info.get("pid"), int):
        return False
    started = process_start_time(info["pid"])
    if started is None:
        return False
    return info.get("started") in (None, 0) or started == 0 or started == info.get("started")


def held_by_other(cfg):
    """回傳別人持有的有效鎖資訊；沒有或是自己（同 token）回 None。"""
    info = read_lock(cfg)
    if not lock_alive(info):
        return None
    if os.environ.get(TOKEN_ENV) and info.get("token") == os.environ.get(TOKEN_ENV):
        return None
    if info.get("pid") == os.getpid():
        return None
    return info


def _create(cfg, purpose):
    path = lock_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {
        "pid": os.getpid(),
        "started": process_start_time(os.getpid()),
        "token": secrets.token_hex(8),
        "purpose": purpose,
        "since": now_str(),
        "argv": " ".join(sys.argv[:3]),
    }
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # 原子建立：兩個程序同時搶只有一個成功
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(info, ensure_ascii=False))
    return info


def acquire(cfg, purpose):
    """拿鎖；回傳 (info, 是否新建)。沿用 hours 的 token 時不新建。殘留舊鎖自動清除後重試一次。"""
    info = read_lock(cfg)
    token = os.environ.get(TOKEN_ENV)
    if info and token and info.get("token") == token and lock_alive(info):
        return info, False
    for attempt in (1, 2):
        try:
            return _create(cfg, purpose), True
        except FileExistsError:
            current = read_lock(cfg)
            if lock_alive(current) or attempt == 2:
                owner = current or {}
                raise LockBusy(
                    f"已有另一個程序在執行（PID {owner.get('pid', '?')}，{owner.get('purpose', '?')}，"
                    f"自 {owner.get('since', '?')}）；同一專案一次只能跑一個 run／hours"
                ) from None
            print(f"    （清除殘留的舊鎖：PID {(current or {}).get('pid', '?')} 已不存在）")
            lock_path(cfg).unlink(missing_ok=True)
    raise AssertionError("unreachable")


def release(cfg, info):
    current = read_lock(cfg)
    if current and current.get("token") == info.get("token"):
        lock_path(cfg).unlink(missing_ok=True)


@contextmanager
def holding(cfg, purpose):
    info, created = acquire(cfg, purpose)
    try:
        yield info
    finally:
        if created:
            release(cfg, info)
