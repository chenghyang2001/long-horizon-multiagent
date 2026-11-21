"""引擎共用常數與小工具：時間格式、結束碼、匯率、原子寫檔（Windows 被占用時退避重試）。"""

import os
import time
from datetime import datetime

# 結束碼（run 與 hours 共用；README 有完整說明）
EXIT_OK = 0
EXIT_UNIT_FAILED = 1  # 有單位被判 failed／rejected（品質問題，不是引擎壞掉）
EXIT_CONFIG = 2  # 設定錯誤拒絕啟動；hours 用它表示「引擎錯誤連發而停止」
EXIT_ENGINE_ERROR = 3  # run：本輪遇到引擎錯誤（模型呼叫失敗、檔案缺漏…），單位停在原站下次重試
EXIT_LOCKED = 4  # 已有另一個 run／hours 在跑
EXIT_REFUSED = 5  # 人工指令（approve／reset…）從引擎底下的程序發出，拒絕
# 工作目錄控制檔（git hooks／危險 git 設定／.claude／CLAUDE.md…）被改動：run 與 hours 都停下等人看，
# 不再做下一個單位（下一個 commit 站會執行被植入的 hook）
EXIT_CONTROL_CHANGED = 6
# 所有引擎啟動的子程序（指令站、claude -p）都帶這個旗標；不用 LH_ 開頭，維持「指令站沒有任何 LH_*」的約定
IN_ENGINE_ENV = "LHF_IN_ENGINE"
EXIT_INTERRUPTED = 130

# 只為了讓摘要好讀的概略匯率；走 Max 訂閱不實扣，這是「等值成本」
USD_TO_TWD = 32.0

# 巢狀 session 變數會讓子 claude 以為自己在別的 session 裡；以下幾個是正常設定要保留
KEEP_CLAUDE_ENV = {"CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
# 通知金鑰只給引擎自己（notify.py）用；代理、指令站、git hook 拿到就能把 bot 拿去亂發或外流。
# 整個 TELEGRAM_ 開頭都拿掉（使用者電腦上還有 TELEGRAM_CHAT_ID_<名字> 之類的變數）
NOTIFY_SECRET_PREFIX = "TELEGRAM_"
# 引擎內部變數（LH_LOCK_TOKEN 拿到就能沿用 hours 的鎖改 state.json；LH_* 讓 gate 判斷角色）一律不外流
ENGINE_ENV_PREFIXES = ("LH_", "LHF_")

REPLACE_RETRIES = 5
REPLACE_BASE_WAIT = 0.05


def strip_nested_env(env):
    """就地移除：API key（成本鐵律一律走 Max 訂閱）、TELEGRAM_*、巢狀 Claude session 變數。"""
    env.pop("ANTHROPIC_API_KEY", None)
    for key in [k for k in env if k.upper().startswith(NOTIFY_SECRET_PREFIX)]:
        env.pop(key)
    for key in list(env):
        if key in KEEP_CLAUDE_ENV:
            continue
        if key in ("CLAUDECODE", "CLAUDE_PID") or key.startswith("CLAUDE_CODE_"):
            env.pop(key)


def scrubbed_env(in_engine=True):
    """引擎啟動「不需要任何引擎資訊」的子程序（git、claude --help）用的環境：
    strip_nested_env＋移除全部 LH_*／LHF_*。in_engine=True 時再設回 LHF_IN_ENGINE=1：
    git hook（或 hook 叫起來的程式）若想執行 lhf approve／reset，CLI 第一層檢查就會擋下；
    這個旗標本身不給任何權限，只會讓人工指令被拒，所以保留比拿掉安全。"""
    env = os.environ.copy()
    strip_nested_env(env)
    for key in [k for k in env if k.upper().startswith(ENGINE_ENV_PREFIXES)]:
        env.pop(key)
    env["GIT_TERMINAL_PROMPT"] = "0"  # 無人值守：git 要帳密就直接失敗，不要卡住等輸入
    if in_engine:
        env[IN_ENGINE_ENV] = "1"
    return env


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def tmp_name(path):
    """暫存檔名帶 PID：兩個程序同時寫同一檔時不會互相踩到對方的 .tmp。"""
    return path.with_name(f"{path.name}.{os.getpid()}.tmp")


def atomic_replace(tmp_path, path):
    """os.replace 在 Windows 上遇到防毒或其他程序開著目標檔會 PermissionError：退避重試 5 次。"""
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(REPLACE_BASE_WAIT * (2**attempt))


def atomic_write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_name(path)
    tmp_path.write_text(text, encoding="utf-8")
    atomic_replace(tmp_path, path)
