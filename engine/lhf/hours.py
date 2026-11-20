"""連續跑多輪（長時間無人值守）：每輪用子程序執行 `python -m lhf <project> run`。

輸出即時轉印到畫面並附加寫進 run/logs/hours-<開始時間>.log；結束時寫 run/logs/hours-summary-<開始時間>.md。
停止條件（任一成立）：
  (a) 經過時間 ≥ --hours：不砍正在跑的那一輪，只是不再開新的一輪
  (b) 沒有可執行的單位（全部結束、等待核准、或依賴未完成）
  (c) 連續 2 輪結束碼非 0：醒目警告並寫 run/ALERT.txt
  (d) Ctrl+C：等子程序存檔收尾後停止，exit 130
  (e) 達到 --max-rounds
"""

import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from lhf.config import ENGINE_DIR
from lhf.errors import RoundError
from lhf.state import alerts_path, load_state, read_jsonl, run_path, runnable_units

MAX_CONSECUTIVE_FAILS = 2
EXIT_INTERRUPTED = 130
# 子程序收到 Ctrl+C 後要寫 state.json／heartbeat，給它時間收尾，太久才強制終止
INTERRUPT_GRACE_SEC = 60
USD_TO_TWD = 32.0


# ---------- 輸出 ----------


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


def safe_state(cfg):
    """讀不到就回 None，讓迴圈以「state.json 讀不到」停下，不在壞狀態上硬跑。"""
    try:
        return load_state(cfg)
    except RoundError as exc:
        print(f"（{exc}）", file=sys.stderr)
        return None


def unit_rec(state, unit):
    return (state or {}).get("units", {}).get(unit, {})


def done_counts(state):
    queue = (state or {}).get("queue", [])
    return sum(1 for unit in queue if unit_rec(state, unit).get("status") == "done"), len(queue)


def read_heartbeat(cfg, since):
    path = run_path(cfg, "heartbeat.txt")
    try:
        text = path.read_text(encoding="utf-8").strip()
        stale = path.stat().st_mtime < since
    except OSError as exc:
        return f"（讀不到 heartbeat：{exc}）"
    return text + ("（本輪未更新）" if stale else "")


# ---------- 子程序 ----------


def child_env():
    env = os.environ.copy()
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"})
    env["PYTHONPATH"] = str(ENGINE_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def stop_child(proc, ctx):
    """Windows 主控台的 Ctrl+C 本來就會同時送到子程序；POSIX 再補送 SIGINT。"""
    say(ctx, "\n⛔ 收到 Ctrl+C，等子程序存檔收尾…")
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


def stream_round(ctx):
    """回傳 (exit code, 是否被 Ctrl+C 中斷)。stderr 併進 stdout，順序才與畫面一致。"""
    cmd = [sys.executable, "-m", "lhf", str(ctx["cfg"].project_dir), "run"]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=ENGINE_DIR,
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        say(ctx, f"錯誤：無法啟動本輪子程序：{exc}")
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
    """本輪 claude -p 次數＝前後差值（接續的單位不重算）。"""
    cfg = ctx["cfg"]
    before = safe_state(cfg) or {}
    candidates = runnable_units(cfg, before) if before else []
    unit = candidates[0] if candidates else "-"
    calls_before = unit_rec(before, unit).get("calls", 0)
    no = len(ctx["rounds"]) + 1
    say(ctx, f"\n{'#' * 60}\n# 第 {no} 輪開始｜{datetime.now():%Y-%m-%d %H:%M:%S}｜預計單位：{unit}\n{'#' * 60}")
    start_wall, start = time.time(), time.monotonic()
    code, interrupted = stream_round(ctx)
    after = safe_state(cfg) or {}
    rec = unit_rec(after, unit)
    record = {
        "no": no,
        "unit": unit,
        "exit": code,
        "seconds": time.monotonic() - start,
        "status": rec.get("status", "?"),
        "calls": rec.get("calls", 0) - calls_before,
        "heartbeat": read_heartbeat(cfg, start_wall),
    }
    ctx["rounds"].append(record)
    report_progress(ctx, no, code, after)
    return record, interrupted


def report_progress(ctx, no, code, state):
    elapsed = time.monotonic() - ctx["start"]
    done, total = done_counts(state)
    say(
        ctx,
        f"第 {no} 輪結束｜exit={code}｜已經過 {fmt_hm(elapsed)}｜剩餘 {fmt_hm(ctx['limit_sec'] - elapsed)}"
        f"｜完成單位 {done}/{total}",
    )


def consecutive_fails(ctx):
    count = 0
    for record in reversed(ctx["rounds"]):
        if record["exit"] == 0:
            break
        count += 1
    return count


def raise_alert(ctx):
    """健康檢查失敗：醒目警告＋ALERT.txt（給隔天早上一眼就看到）。"""
    path = run_path(ctx["cfg"], "ALERT.txt")
    lines = [
        f"時間：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"原因：連續 {MAX_CONSECUTIVE_FAILS} 輪結束碼非 0，已停止連續執行",
        f"log：{ctx['log_path']}",
        "最後兩輪：",
    ]
    lines += [
        f"  第 {r['no']} 輪｜{r['unit']}｜exit={r['exit']}｜heartbeat：{r['heartbeat']}"
        for r in ctx["rounds"][-MAX_CONSECUTIVE_FAILS:]
    ]
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        say(ctx, f"（寫 ALERT.txt 失敗：{exc}）")
    bar = "!" * 60
    say(ctx, f"\n{bar}\n!! 警告：連續 {MAX_CONSECUTIVE_FAILS} 輪失敗，停止連續執行。詳見 {path}\n{bar}")
    for line in lines:
        say(ctx, "!! " + line)


# ---------- 主迴圈 ----------


def stop_reason(args, ctx):
    if time.monotonic() - ctx["start"] >= ctx["limit_sec"]:
        return f"時間到（{args.hours} 小時）"
    if args.max_rounds is not None and len(ctx["rounds"]) >= args.max_rounds:
        return f"已達 --max-rounds {args.max_rounds}"
    state = safe_state(ctx["cfg"])
    if state is None:
        return "state.json 讀不到"
    if not runnable_units(ctx["cfg"], state):
        return "沒有可執行的工作單位"
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


def touched_units(ctx):
    return list(dict.fromkeys(r["unit"] for r in ctx["rounds"] if r["unit"] != "-"))


def rework_lines(state, units):
    """每個單位退回幾次、原因（規格 §9 紀錄要求）。"""
    lines = [
        "",
        "## 退回紀錄",
        "",
        "| 單位 | 最終狀態 | 退回次數 | 失敗判定（站：原因） |",
        "| --- | --- | --- | --- |",
    ]
    for unit in units:
        rec = unit_rec(state, unit)
        retries = "、".join(f"{k} {v}" for k, v in (rec.get("retries") or {}).items()) or "0"
        fails = [
            f"{v['station']} {v['verdict']}{('：' + v['note']) if v.get('note') else ''}"
            for v in rec.get("verdicts") or []
            if v.get("verdict") != "PASS"
        ]
        lines.append(f"| {unit} | {rec.get('status', '?')} | {retries} | {'；'.join(fails) or '—'} |")
    return lines


def summary_header(ctx, reason, code, state):
    done, total = done_counts(state)
    return [
        f"# 連續執行摘要 {ctx['cfg'].name} {ctx['stamp']}",
        "",
        f"- 開始：{ctx['start_dt']:%Y-%m-%d %H:%M:%S}",
        f"- 結束：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 總時長：{fmt_hm(time.monotonic() - ctx['start'])}（HH:MM）",
        f"- 輪數：{len(ctx['rounds'])}",
        f"- 停止原因：{reason}（exit {code}）",
        f"- 完成單位：{done}/{total}",
        f"- log：`{ctx['log_path'].name}`",
        "",
        "| 輪 | 單位 | 結果 | exit | 耗時 | 本輪 claude -p 次數 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]


def summary_markdown(ctx, reason, code):
    state = safe_state(ctx["cfg"]) or {}
    units = touched_units(ctx)
    calls = sum(unit_rec(state, unit).get("calls", 0) for unit in units)
    cost = sum(float(unit_rec(state, unit).get("cost_usd") or 0.0) for unit in units)
    lines = summary_header(ctx, reason, code, state)
    lines += [
        f"| {r['no']} | {r['unit']} | {r['status']} | {r['exit']} | {fmt_hm(r['seconds'])}"
        f"（{r['seconds']:.0f} 秒） | {r['calls']} |"
        for r in ctx["rounds"]
    ]
    lines += rework_lines(state, units)
    alerts = [a for a in read_jsonl(alerts_path(ctx["cfg"])) if a.get("unit") in units]
    lines.append("")
    lines.append(f"- ALERT 清單（本次單位）：{len(alerts)} 筆")
    lines += [f"  - {a['unit']} {a['status']}：{a['reason']}" for a in alerts]
    lines += [
        f"- 總 claude -p 次數（這些單位累計）：{calls}",
        f"- 等值成本：US$ {cost:.4f}（約 NT$ {cost * USD_TO_TWD:.1f}，走 Max 訂閱不實扣）",
    ]
    return "\n".join(lines) + "\n"


def write_summary(ctx, reason, code):
    path = run_path(ctx["cfg"], "logs") / f"hours-summary-{ctx['stamp']}.md"
    try:
        path.write_text(summary_markdown(ctx, reason, code), encoding="utf-8")
    except OSError as exc:
        say(ctx, f"（寫摘要失敗：{exc}）")
        return
    say(ctx, f"\n{'=' * 60}\n停止：{reason}｜共 {len(ctx['rounds'])} 輪｜摘要：{path}\n{'=' * 60}")


# ---------- 進入點 ----------


def cmd_hours(cfg, args):
    start_dt = datetime.now()
    logs_dir = run_path(cfg, "logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"hours-{start_dt:%Y%m%d-%H%M%S}.log"
    with log_path.open("a", encoding="utf-8") as log_fh:
        ctx = {
            "cfg": cfg,
            "start": time.monotonic(),
            "start_dt": start_dt,
            "stamp": f"{start_dt:%Y%m%d-%H%M%S}",
            "limit_sec": args.hours * 3600,
            "rounds": [],
            "log": log_fh,
            "log_path": log_path,
        }
        say(
            ctx, f"連續執行開始：{cfg.name}｜{args.hours} 小時、max-rounds={args.max_rounds}、gap={args.gap_seconds} 秒"
        )
        reason, code = "未知", 1
        try:
            reason, code = run_loop(args, ctx)
        except KeyboardInterrupt:
            reason, code = "Ctrl+C 中斷", EXIT_INTERRUPTED
        finally:
            write_summary(ctx, reason, code)
        return code
