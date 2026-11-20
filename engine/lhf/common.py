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

REPLACE_RETRIES = 5
REPLACE_BASE_WAIT = 0.05


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
