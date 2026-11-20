"""模擬寄出研究報告：把 report.md 複製到 outbox/sent-report.md，不真的寄信。

這是 policy.yaml 裡要人工核准的 send_report 動作；真的對外寄送前先用模擬版練習閘門。
"""
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
REPORT_PATH = BASE / "report.md"
OUTBOX_DIR = BASE / "outbox"
SENT_PATH = OUTBOX_DIR / "sent-report.md"


def main():
    """讀報告、加上寄出時間、寫進 outbox。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if not REPORT_PATH.exists():
        print(f"錯誤：找不到 {REPORT_PATH.name}，請先寫好報告", file=sys.stderr)
        return 1
    try:
        body = REPORT_PATH.read_text(encoding="utf-8")
        OUTBOX_DIR.mkdir(exist_ok=True)
        sent_at = datetime.now().isoformat(timespec="seconds")
        SENT_PATH.write_text(f"<!-- 寄出時間：{sent_at} -->\n\n{body}", encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"錯誤：模擬寄出失敗：{error}", file=sys.stderr)
        return 1
    print(f"已模擬寄出：{SENT_PATH.relative_to(BASE)}（{sent_at}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
