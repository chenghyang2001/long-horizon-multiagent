"""K143 演練備課工廠：連續跑多輪（給 i5 長時間無人值守用）。

每輪用子程序執行 run_round.py（一次＝一輪＝一章），輸出即時轉印到畫面並附加寫進
logs/hours-<開始時間>.log；結束時寫 logs/hours-summary-<開始時間>.md。

停止條件（任一成立）：
  (a) 經過時間 ≥ --hours：不砍正在跑的那一輪，只是不再開新的一輪
  (b) queue 沒有待做的章（state.json 中 status 不是 done／failed／rejected 的章數為 0）
  (c) 連續 2 輪 exit code 非 0：印醒目警告並寫 ALERT.txt
  (d) Ctrl+C：等子程序存檔收尾後停止，exit 130
  (e) 達到 --max-rounds

用法（Git Bash）：
  PYTHONUTF8=1 python run_hours.py --hours 2 [--max-rounds N] [--gap-seconds 30]
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CAPSTONE = Path(__file__).resolve().parent
RUN_ROUND = CAPSTONE / "run_round.py"
STATE_PATH = CAPSTONE / "state.json"
HEARTBEAT_PATH = CAPSTONE / "heartbeat.txt"
ALERT_PATH = CAPSTONE / "ALERT.txt"
LOGS_DIR = CAPSTONE / "logs"

FINAL_STATUSES = {"done", "failed", "rejected"}
MAX_CONSECUTIVE_FAILS = 2
EXIT_INTERRUPTED = 130
# 子程序收到 Ctrl+C 後要寫 state.json／heartbeat，給它時間收尾，太久才強制終止
INTERRUPT_GRACE_SEC = 60
USD_TO_TWD = 32.0


# ---------- 輸出（畫面＋log 同步） ----------

def say(ctx, text=""):
    """畫面與 log 同步寫；log 每行 flush，斷電時最多只掉最後一行。"""
    print(text, flush=True)
    ctx["log"].write(text + "\n")
    ctx["log"].flush()


def echo_raw(ctx, chunk):
    sys.stdout.write(chunk)
    sys.stdout.flush()
    ctx["log"].write(chunk)
    ctx["log"].flush()


def fmt_hm(seconds):
    minutes = max(0, int(seconds)) // 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ---------- 狀態 ----------

def load_state():
    """讀不到就回 None，讓迴圈以「state.json 讀不到」停下，不在壞狀態上硬跑。"""
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"（讀取 state.json 失敗：{exc}）", file=sys.stderr)
        return None


def pending_chapters(state):
    chapters = state.get("chapters", {})
    return [ch for ch in state.get("queue", [])
            if chapters.get(ch, {}).get("status") not in FINAL_STATUSES]


def chapter_rec(state, ch):
    return (state or {}).get("chapters", {}).get(ch, {})


def done_counts(state):
    queue = (state or {}).get("queue", [])
    done = sum(1 for ch in queue if chapter_rec(state, ch).get("status") == "done")
    return done, len(queue)


def read_heartbeat(since):
    """heartbeat 沒在本輪更新（例如 run_round 在寫 heartbeat 前就出錯）要標出來，避免誤讀舊資料。"""
    try:
        text = HEARTBEAT_PATH.read_text(encoding="utf-8").strip()
        stale = HEARTBEAT_PATH.stat().st_mtime < since
    except OSError as exc:
        return f"（讀不到 heartbeat：{exc}）"
    return text + ("（本輪未更新）" if stale else "")


# ---------- 子程序 ----------

def child_env():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def stop_child(proc, ctx):
    """Windows 主控台的 Ctrl+C 本來就會同時送到子程序；POSIX（例如被 nohup 脫離終端）再補送 SIGINT。"""
    say(ctx, "\n⛔ 收到 Ctrl+C，等 run_round.py 存檔收尾…")
    if os.name != "nt" and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
    try:
        rest, _ = proc.communicate(timeout=INTERRUPT_GRACE_SEC)
    except subprocess.TimeoutExpired:
        say(ctx, f"（子程序 {INTERRUPT_GRACE_SEC} 秒內沒結束，強制終止）")
        proc.kill()
        rest, _ = proc.communicate()
    except KeyboardInterrupt:
        proc.kill()
        rest, _ = proc.communicate()
    if rest:
        echo_raw(ctx, rest)
    return proc.returncode if proc.returncode is not None else EXIT_INTERRUPTED


def stream_round(ctx):
    """回傳 (exit code, 是否被 Ctrl+C 中斷)。stderr 併進 stdout，順序才與畫面一致。"""
    cmd = [sys.executable, str(RUN_ROUND)]
    try:
        proc = subprocess.Popen(cmd, cwd=CAPSTONE, env=child_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        say(ctx, f"錯誤：無法啟動 run_round.py：{exc}")
        return 1, False
    try:
        for line in proc.stdout:
            echo_raw(ctx, line)
        return proc.wait(), False
    except KeyboardInterrupt:
        stop_child(proc, ctx)
        return EXIT_INTERRUPTED, True


# ---------- 一輪 ----------

def run_one_round(ctx):
    """跑一輪並從 state.json 補上章、結果、本輪 claude -p 次數（前後差值，接續的章不重算）。"""
    before = load_state() or {}
    pending = pending_chapters(before)
    ch = pending[0] if pending else "-"
    calls_before = chapter_rec(before, ch).get("calls", 0)
    no = len(ctx["rounds"]) + 1
    say(ctx, f"\n{'#' * 60}\n# 第 {no} 輪開始｜{datetime.now():%Y-%m-%d %H:%M:%S}｜預計章：{ch}\n{'#' * 60}")
    start_wall, start = time.time(), time.monotonic()
    code, interrupted = stream_round(ctx)
    after = load_state() or {}
    rec = chapter_rec(after, ch)
    record = {"no": no, "chapter": ch, "exit": code, "seconds": time.monotonic() - start,
              "status": rec.get("status", "?"), "calls": rec.get("calls", 0) - calls_before,
              "heartbeat": read_heartbeat(start_wall)}
    ctx["rounds"].append(record)
    print_progress(ctx, record, after)
    return record, interrupted


def print_progress(ctx, record, state):
    elapsed = time.monotonic() - ctx["start"]
    remaining = ctx["limit_sec"] - elapsed
    done, total = done_counts(state)
    say(ctx, f"第 {record['no']} 輪結束｜exit={record['exit']}｜已經過 {fmt_hm(elapsed)}"
             f"｜剩餘 {fmt_hm(remaining)}｜完成章數 {done}/{total}")


def consecutive_fails(ctx):
    count = 0
    for record in reversed(ctx["rounds"]):
        if record["exit"] == 0:
            break
        count += 1
    return count


def raise_alert(ctx):
    """健康檢查失敗：醒目警告＋ALERT.txt（給隔天早上一眼就看到）。"""
    last_two = ctx["rounds"][-MAX_CONSECUTIVE_FAILS:]
    lines = [f"時間：{datetime.now():%Y-%m-%d %H:%M:%S}",
             f"原因：連續 {MAX_CONSECUTIVE_FAILS} 輪 run_round.py 結束碼非 0，已停止連續執行",
             f"log：{ctx['log_path']}", "最後兩輪："]
    lines += [f"  第 {r['no']} 輪｜{r['chapter']}｜exit={r['exit']}｜heartbeat：{r['heartbeat']}"
              for r in last_two]
    try:
        ALERT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        say(ctx, f"（寫 ALERT.txt 失敗：{exc}）")
    bar = "!" * 60
    say(ctx, f"\n{bar}\n!! 警告：連續 {MAX_CONSECUTIVE_FAILS} 輪失敗，停止連續執行。詳見 {ALERT_PATH}\n{bar}")
    for line in lines:
        say(ctx, "!! " + line)


# ---------- 主迴圈 ----------

def stop_reason(args, ctx):
    if time.monotonic() - ctx["start"] >= ctx["limit_sec"]:
        return f"時間到（{args.hours} 小時）"
    if args.max_rounds is not None and len(ctx["rounds"]) >= args.max_rounds:
        return f"已達 --max-rounds {args.max_rounds}"
    state = load_state()
    if state is None:
        return "state.json 讀不到"
    if not pending_chapters(state):
        return "queue 裡沒有待做的章"
    return None


def wait_gap(args, ctx):
    """回傳 True＝等待中被 Ctrl+C。"""
    if args.gap_seconds <= 0:
        return False
    say(ctx, f"（等 {args.gap_seconds} 秒再開下一輪）")
    try:
        time.sleep(args.gap_seconds)
    except KeyboardInterrupt:
        say(ctx, "\n⛔ 等待中收到 Ctrl+C")
        return True
    return False


def run_loop(args, ctx):
    """回傳 (停止原因, exit code)。"""
    while True:
        reason = stop_reason(args, ctx)
        if reason:
            return reason, 0
        _, interrupted = run_one_round(ctx)
        if interrupted:
            return "Ctrl+C 中斷", EXIT_INTERRUPTED
        if consecutive_fails(ctx) >= MAX_CONSECUTIVE_FAILS:
            raise_alert(ctx)
            return f"連續 {MAX_CONSECUTIVE_FAILS} 輪失敗", 1
        if stop_reason(args, ctx) is None and wait_gap(args, ctx):
            return "Ctrl+C 中斷（等待中）", EXIT_INTERRUPTED


# ---------- 摘要 ----------

def summary_totals(ctx, state):
    """總次數／成本以本次碰過的章為單位，從 state.json 每章 calls、cost_usd 加總。"""
    touched = list(dict.fromkeys(r["chapter"] for r in ctx["rounds"] if r["chapter"] != "-"))
    calls = sum(chapter_rec(state, ch).get("calls", 0) for ch in touched)
    cost = sum(float(chapter_rec(state, ch).get("cost_usd") or 0.0) for ch in touched)
    return touched, calls, cost


def summary_markdown(ctx, reason, code):
    state = load_state() or {}
    touched, calls, cost = summary_totals(ctx, state)
    done, total = done_counts(state)
    end = datetime.now()
    lines = [f"# 連續執行摘要 {ctx['stamp']}", "",
             f"- 開始：{ctx['start_dt']:%Y-%m-%d %H:%M:%S}", f"- 結束：{end:%Y-%m-%d %H:%M:%S}",
             f"- 總時長：{fmt_hm(time.monotonic() - ctx['start'])}（HH:MM）",
             f"- 輪數：{len(ctx['rounds'])}", f"- 停止原因：{reason}（exit {code}）",
             f"- 完成章數：{done}/{total}", f"- log：`{ctx['log_path'].name}`", "",
             "| 輪 | 章 | 結果 | exit | 耗時 | 本輪 claude -p 次數 |", "| --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {r['no']} | {r['chapter']} | {r['status']} | {r['exit']} | {fmt_hm(r['seconds'])}"
              f"（{r['seconds']:.0f} 秒） | {r['calls']} |" for r in ctx["rounds"]]
    lines += ["", f"- 本次碰過的章：{'、'.join(touched) or '（無）'}",
              f"- 總 claude -p 次數（這些章累計）：{calls}",
              f"- 等值成本：US$ {cost:.4f}（約 NT$ {cost * USD_TO_TWD:.1f}，走 Max 訂閱不實扣）"]
    return "\n".join(lines) + "\n"


def write_summary(ctx, reason, code):
    path = LOGS_DIR / f"hours-summary-{ctx['stamp']}.md"
    try:
        path.write_text(summary_markdown(ctx, reason, code), encoding="utf-8")
    except OSError as exc:
        say(ctx, f"（寫摘要失敗：{exc}）")
        return
    say(ctx, f"\n{'=' * 60}\n停止：{reason}｜共 {len(ctx['rounds'])} 輪｜摘要：{path}\n{'=' * 60}")


# ---------- 進入點 ----------

def parse_args(argv):
    parser = argparse.ArgumentParser(description="連續跑多輪 run_round.py（長時間無人值守）")
    parser.add_argument("--hours", type=float, required=True, help="最多跑幾小時（到時不砍正在跑的輪）")
    parser.add_argument("--max-rounds", type=int, default=None, help="最多跑幾輪")
    parser.add_argument("--gap-seconds", type=float, default=30, help="每輪之間等幾秒（預設 30）")
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("--hours 要大於 0")
    if args.max_rounds is not None and args.max_rounds < 1:
        parser.error("--max-rounds 要 ≥ 1")
    if args.gap_seconds < 0:
        parser.error("--gap-seconds 不可為負")
    return args


def make_ctx(args, log_fh, log_path, start_dt):
    return {"start": time.monotonic(), "start_dt": start_dt, "stamp": f"{start_dt:%Y%m%d-%H%M%S}",
            "limit_sec": args.hours * 3600, "rounds": [], "log": log_fh, "log_path": log_path}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    start_dt = datetime.now()
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        log_path = LOGS_DIR / f"hours-{start_dt:%Y%m%d-%H%M%S}.log"
        with log_path.open("a", encoding="utf-8") as log_fh:
            ctx = make_ctx(args, log_fh, log_path, start_dt)
            say(ctx, f"連續執行開始：{args.hours} 小時、max-rounds={args.max_rounds}、gap={args.gap_seconds} 秒")
            reason, code = "未知", 1
            try:
                reason, code = run_loop(args, ctx)
            except KeyboardInterrupt:
                reason, code = "Ctrl+C 中斷", EXIT_INTERRUPTED
            finally:
                write_summary(ctx, reason, code)
            return code
    except OSError as exc:
        print(f"錯誤：檔案操作失敗：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
