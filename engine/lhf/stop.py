"""工廠停止：人從獨立終端（或 skill 的 Git Bash）請正在跑的 run／hours 收尾後停下。

為什麼不直接送 CTRL_BREAK：GenerateConsoleCtrlEvent 只能送給「同一個主控台」裡的程序群組；hours 跑在
Windows Terminal 另一個視窗（另一個主控台），skill 的 Bash 送不過去。用 FreeConsole＋AttachConsole 換到對方
主控台再送的做法，會把訊號送給那個主控台的所有程序（含正在跑的 claude），呼叫端也會失去自己的主控台，不穩。
所以改用檔案（run/.runtime/stop-request.json）：
  - run 每站開始前看到就停（進度在每站結束時已存好，下次從這一站接著做）
  - hours 每輪結束、輪與輪之間等待時看到就停；mode=now 時，hours 在本輪執行中就對「自己的」子程序送
    CTRL_BREAK（同一個主控台，送得到），子程序把目前這站當成中斷、存檔後結束
  - 等不到（--wait 秒）才 taskkill /T /F 持鎖程序整棵樹（Job Object 的 KILL_ON_JOB_CLOSE 會帶走所有子孫）
停止要求檔不列入指令站前後比對（人可能在指令站執行中下 stop），由持鎖程序在開始與結束時清掉。
"""

import json
import locale
import os
import signal
import subprocess
import time

from lhf import integrity, lock
from lhf.common import EXIT_ENGINE_ERROR, EXIT_LOCKED, EXIT_OK, EXIT_UNIT_FAILED, atomic_write_text, now_str

MODES = ("station", "now")
POLL_SEC = 1.0
KILL_CONFIRM_SEC = 15


def request_path(cfg):
    return cfg.run_dir / ".runtime" / integrity.STOP_REQUEST_NAME


def read_request(cfg):
    """回傳停止要求（dict）；沒有回 None。讀不懂（寫到一半）當成一般停止要求。"""
    try:
        data = json.loads(request_path(cfg).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"mode": "station"}
    if not isinstance(data, dict) or data.get("mode") not in MODES:
        return {"mode": "station"}
    return data


def requested(cfg):
    return read_request(cfg) is not None


def wants_now(cfg):
    return (read_request(cfg) or {}).get("mode") == "now"


def clear_request(cfg, note=None):
    if not request_path(cfg).exists():
        return False
    integrity.safe_unlink(request_path(cfg))
    if note:
        print(f"    （{note}）", flush=True)
    return True


def write_request(cfg, mode, owner):
    record = {"ts": now_str(), "mode": mode, "requester_pid": os.getpid(), "lock_pid": owner.get("pid")}
    atomic_write_text(request_path(cfg), json.dumps(record, ensure_ascii=False))


# ---------- stop 子指令 ----------


def same_owner(cfg, owner):
    """鎖還在、而且還是同一個程序（pid＋啟動時間相同）。"""
    current = lock.read_lock(cfg)
    return (
        lock.lock_alive(current)
        and current.get("pid") == owner.get("pid")
        and current.get("started") == owner.get("started")
    )


def wait_released(cfg, owner, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not same_owner(cfg, owner):
            return True
        time.sleep(POLL_SEC)
    return not same_owner(cfg, owner)


def kill_tree(pid):
    """Windows：taskkill /T /F；POSIX：SIGTERM，10 秒後還在就 SIGKILL。回傳 (成功與否, 說明)。"""
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"taskkill 無法執行：{exc}"
        output = decode_console(result.stdout) or decode_console(result.stderr)
        return result.returncode == 0, output.strip()[:300]
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(1)
            os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, "已結束"
    except OSError as exc:
        return False, str(exc)
    return True, "已送 SIGKILL"


def console_encoding():
    """taskkill 輸出用系統主控台代碼頁（繁中 Windows＝cp950），用 UTF-8 解會變亂碼。
    不可用 locale.getpreferredencoding：引擎一律在 PYTHONUTF8=1 下跑，它會回 utf-8。"""
    if os.name == "nt":
        return "oem"
    return locale.getencoding()


def decode_console(data):
    if not data:
        return ""
    try:
        return data.decode(console_encoding(), errors="replace")
    except LookupError:
        return data.decode("mbcs" if os.name == "nt" else "utf-8", errors="replace")


def force_stop(cfg, owner):
    """收尾逾時：確認還是同一個程序才殺（PID 可能已被重用），殺完確認鎖的主人真的不在了。"""
    if not same_owner(cfg, owner):
        return EXIT_OK
    print(f"等不到收尾，強制結束 PID {owner['pid']} 整棵程序樹（目前這站下次重做）", flush=True)
    ok, detail = kill_tree(owner["pid"])
    if ok and wait_released(cfg, owner, KILL_CONFIRM_SEC):
        clear_request(cfg)
        print(f"已強制結束（{detail}）；殘留的鎖與標記下次啟動時會自動清除", flush=True)
        return EXIT_UNIT_FAILED
    print(f"強制結束失敗：{detail}", flush=True)
    return EXIT_ENGINE_ERROR


def cmd_stop(cfg, now=False, wait=300.0, force=False):
    """結束碼：0＝已停止（或本來就沒在跑、或 --wait 0 只送出要求）；4＝等完 --wait 還在收尾（沒加 --force，
    不強制；停止要求仍有效，它做完這站就會停）；1＝收尾逾時、已強制結束（--force）；3＝強制結束失敗。
    預設不強制：agent 站一站可能十幾分鐘，強制結束會丟掉整站的進度與已花的額度。"""
    owner = lock.read_lock(cfg)
    if not lock.lock_alive(owner):
        clear_request(cfg, "清除殘留的停止要求")
        print("沒有執行中的 run／hours")
        return EXIT_OK
    mode = "now" if now else "station"
    write_request(cfg, mode, owner)
    when = "目前這一站會中斷（下次重做這站）" if now else "目前這一站做完後停下"
    print(f"已送出停止要求給 PID {owner.get('pid')}（{owner.get('purpose')}）：{when}", flush=True)
    if wait <= 0:
        return EXIT_OK
    if wait_released(cfg, owner, wait):
        print("已停止（進度已存，下次執行會從停下的那一站接著做）", flush=True)
        return EXIT_OK
    if not force:
        print(f"等了 {wait:.0f} 秒還在收尾（停止要求仍有效）；要強制結束請加 --force", flush=True)
        return EXIT_LOCKED
    return force_stop(cfg, owner)
