"""祖先程序鏈：判斷「目前程序是不是引擎（持鎖程序）的子孫」。

用途：approve／reset 等人工指令若是從引擎底下的程序（指令站跑的撰寫者腳本、claude 的 Bash）發出，
就等於撰寫者自己核准自己，必須拒絕。環境變數旗標可以被 `env -u` 清掉，祖先鏈改不了，所以兩層都做。
Windows：CreateToolhelp32Snapshot 取得全部程序的 PID→父 PID；Windows 的父 PID 可能指向已結束後被重用的
PID，所以往上走時要求「父程序的啟動時間早於子程序」，不符就停止。POSIX：讀 /proc/<pid>/stat。
"""

import os

MAX_DEPTH = 64


def parent_map():
    """回傳 {pid: 父 pid}；拿不到就回空 dict（呼叫端視為「無法證明是子孫」）。"""
    if os.name == "nt":
        return _windows_parent_map()
    return {}


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]


def _windows_parent_map():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return {}
    result = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return result


def posix_parent(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return int(handle.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return os.getppid() if pid == os.getpid() else None


def ancestors(pid=None):
    """從 pid（預設自己）往上列出祖先 PID，不含自己。父程序比子程序晚啟動＝PID 被重用，停止。"""
    from lhf.lock import process_start_time

    pid = os.getpid() if pid is None else pid
    parents = parent_map()
    chain, current = [], pid
    for _ in range(MAX_DEPTH):
        parent = parents.get(current) if parents else posix_parent(current)
        if not parent or parent == current or parent in chain:
            break
        child_start, parent_start = process_start_time(current), process_start_time(parent)
        if parent_start is None or (child_start and parent_start and parent_start > child_start):
            break
        chain.append(parent)
        current = parent
    return chain


def is_descendant_of(pid):
    return isinstance(pid, int) and pid in ancestors()
