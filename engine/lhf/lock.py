"""執行鎖 run/.runtime/run.lock：同一個專案同時只能有一個 run／hours 在改 state.json。

鎖內容＝PID＋該程序的啟動時間＋token。只看 PID 不夠：程序當掉後 PID 可能被別的程序重用，
所以要同時比對啟動時間，不符就視為殘留舊鎖、自動清掉。
hours 拿到鎖後把 token 放進環境變數 LH_LOCK_TOKEN，它啟動的每一輪 run 子程序憑 token 沿用同一把鎖。
"""

import json
import os
import secrets
import sys
import time
from contextlib import contextmanager

from lhf.common import now_str
from lhf.errors import RoundError

LOCK_NAME = "run.lock"
TOKEN_ENV = "LH_LOCK_TOKEN"
CLEANUP_STALE_SEC = 30  # 清舊鎖的互斥檔超過這麼久還在＝清理者當掉了
YOUNG_LOCK_SEC = 10  # 讀不懂但剛建立的鎖檔＝別人正在寫，不可當成舊鎖
ATTEMPTS = 20
# Windows：檔案在「刪除中」狀態時，開啟／建立／連結會回 PermissionError（拒絕存取）而不是「已存在」。
# 這是暫時狀態，視同「現在有人在動這把鎖」，稍等再試，不可讓例外往外丟。
TRANSIENT_WAIT_SEC = 0.05


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
    """先把完整內容寫進暫存檔，再用 os.link 原子地「建立且不覆蓋」：別人永遠讀不到寫一半的鎖。"""
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
    tmp = path.with_name(f"{LOCK_NAME}.{os.getpid()}.{info['token']}.tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    try:
        os.link(tmp, path)  # 目標已存在就丟 FileExistsError
    finally:
        tmp.unlink(missing_ok=True)
    return info


def busy(info):
    owner = info or {}
    return LockBusy(
        f"已有另一個程序在執行（PID {owner.get('pid', '?')}，{owner.get('purpose', '?')}，"
        f"自 {owner.get('since', '?')}）；同一專案一次只能跑一個 run／hours"
    )


def file_age(path):
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def young_unreadable(cfg, info):
    age = file_age(lock_path(cfg))
    return info is None and age is not None and age < YOUNG_LOCK_SEC


def clear_stale(cfg, stale):
    """用互斥檔包住「再確認一次→刪除」：兩個程序同時發現舊鎖時，只有一個會刪，
    而且刪之前再讀一次，確定還是同一把舊鎖（不會誤刪別人剛建立的新鎖）。"""
    mutex = lock_path(cfg).with_name(LOCK_NAME + ".cleanup")
    try:
        os.close(os.open(mutex, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except (FileExistsError, PermissionError):
        if (file_age(mutex) or 0) > CLEANUP_STALE_SEC:
            safe_unlink(mutex)
        time.sleep(0.1)
        return
    try:
        current = read_lock(cfg)
        if current == stale and not lock_alive(current):
            print(f"    （清除殘留的舊鎖：PID {(stale or {}).get('pid', '?')} 已不存在）")
            safe_unlink(lock_path(cfg))
    finally:
        safe_unlink(mutex)


def safe_unlink(path):
    """刪不掉（別人正開著或也在刪）就算了：下一輪重試會再確認狀態。"""
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        pass


def acquire(cfg, purpose):
    """拿鎖；回傳 (info, 是否新建)。沿用 hours 的 token 時不新建。殘留舊鎖安全清除後重試。"""
    info = read_lock(cfg)
    token = os.environ.get(TOKEN_ENV)
    if info and token and info.get("token") == token and lock_alive(info):
        return info, False
    for _ in range(ATTEMPTS):
        try:
            return _create(cfg, purpose), True
        except FileExistsError:
            current = read_lock(cfg)
        except PermissionError:
            time.sleep(TRANSIENT_WAIT_SEC)  # 鎖檔正在被刪：稍後再看
            continue
        if lock_alive(current) or young_unreadable(cfg, current):
            raise busy(current)
        clear_stale(cfg, current)
    raise busy(read_lock(cfg))


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
