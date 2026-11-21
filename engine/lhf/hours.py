"""連續跑多輪（長時間無人值守）：每輪用子程序執行 `python -m lhf <project> run`。

輸出即時轉印到畫面並附加寫進 run/logs/hours-<開始時間>.log；結束時寫 run/logs/hours-summary-<開始時間>.md。
hours 全程持有執行鎖，子程序憑 LH_LOCK_TOKEN 沿用；同一專案不能同時跑兩個 hours／run。
停止條件（任一成立）：
  (a) 經過時間 ≥ --hours：不砍正在跑的那一輪，只是不再開新的一輪
  (b) 沒有可執行的單位（全部結束、等待核准、或依賴未完成）
  (c) 引擎錯誤連發（連續 max_engine_errors＋1 輪結束碼不是 0／1，或本輪超過總時限被殺）
  (c2) state.json 讀不到（損毀）：停止、不覆寫
  (d) Ctrl+C：請子程序存檔收尾後停止
  (d2) 有人執行 stop（run/.runtime/stop-request.json）：本輪（或 --now 時本站）收尾後停止
  (e) 達到 --max-rounds
  (f) 工作目錄控制檔被改動（git hooks、危險 git 設定鍵、.claude、CLAUDE.md…）：開跑時拍一次基準，
      每輪開始前比對；run 子程序在指令站期間發現（結束碼 6）也一樣。停止並寫 ALERT，不再做下一個單位，
      因為下一個 commit 站會執行被植入的 hook
單位被判 failed／rejected 不會讓 hours 停下，會繼續做下一個單位。
每輪總時限＝calls_per_unit×agent_timeout＋指令站在最多退回次數下的時間＋10 分鐘；超過就殺整棵程序樹。
結束碼：0＝正常停止且沒有 failed／rejected 單位；1＝有單位被判 failed／rejected；
        2＝引擎錯誤連發或 state.json 讀不到而停止；6＝工作目錄控制檔被改動而停止；130＝Ctrl+C 或 stop。
        非 0（130 除外）時一定寫 run/ALERT.txt 並列出原因與單位，給無人值守的監看程式一眼判斷。
通知：factory.yaml 的 notify.telegram.on 含 alert → 寫 ALERT.txt 時發；含 finish → 寫結束摘要時發。
"""

import os
import sys
import time
from datetime import datetime

from lhf import integrity, lock, notify, proc, stop
from lhf.common import (
    EXIT_CONFIG,
    EXIT_CONTROL_CHANGED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_UNIT_FAILED,
    USD_TO_TWD,
    atomic_replace,
    stamp,
)
from lhf.config import ENGINE_DIR
from lhf.errors import RoundError
from lhf.state import alerts_path, file_offset, load_state, read_jsonl, run_path, runnable_units, trace_path

STATE_UNREADABLE = "state.json 讀不到"
STOP_REQUESTED = "收到停止要求（stop）"
STOP_POLL_SEC = 1.0  # 本輪執行中多久看一次 stop --now
# 這兩個結束碼代表「品質判定」而不是引擎壞掉；其他（含逾時被殺＝None）都算引擎錯誤
# （EXIT_CONTROL_CHANGED 與停止要求在 after_round 先處理，不會走到連發計數）
QUALITY_EXITS = {EXIT_OK, EXIT_UNIT_FAILED}


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


def child_env(token):
    """只有 hours 自己啟動的 lhf run 子程序拿得到鎖 token；token 不寫進 os.environ，免得其他子程序繼承。"""
    env = os.environ.copy()
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", lock.TOKEN_ENV: token})
    env["PYTHONPATH"] = str(ENGINE_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def stream_round(ctx):
    """回傳 (exit code 或 None, 狀態 ok／timeout／interrupted)。整輪超過總時限就殺整棵程序樹。"""
    cmd = [sys.executable, "-m", "lhf", str(ctx["cfg"].project_dir), "run"]
    limit = ctx["cfg"].round_time_limit()
    try:
        code, status = proc.stream_tree(
            cmd,
            cwd=ENGINE_DIR,
            env=child_env(ctx["token"]),
            timeout=limit,
            on_output=lambda chunk: echo_raw(ctx, chunk) if chunk else None,
            on_tick=lambda: stop_now_requested(ctx),
        )
    except OSError as exc:
        say(ctx, f"錯誤：無法啟動本輪子程序：{exc}")
        return None, "ok"
    if status == "timeout":
        say(ctx, f"{chr(10)}!! 本輪超過總時限 {limit} 秒，已結束整棵程序樹")
        ctx["alert_lines"].append(f"第 {len(ctx['rounds']) + 1} 輪超過總時限 {limit} 秒被強制結束")
    if status == "interrupted":
        say(ctx, f"{chr(10)}⛔ 收到 Ctrl+C，已請子程序存檔收尾")
    elif status == "stopped":
        say(ctx, f"{chr(10)}⛔ 收到 stop --now，已請子程序把目前這站中斷、存檔收尾")
    elif code == EXIT_INTERRUPTED:
        # 子程序自己收到中斷（例如被人從工作管理員送了 Ctrl+C），hours 本身沒收到：記下來，照引擎錯誤處理
        say(ctx, f"{chr(10)}!! 子程序被中斷（exit 130），hours 未收到中斷；本輪以引擎錯誤計")
    return code, status


# ---------- 一輪 ----------


def stop_now_requested(ctx):
    """本輪執行中每秒看一次停止要求；只有 mode=now 才中斷正在跑的站（一般 stop 等本輪的站做完）。"""
    now = time.monotonic()
    if now - ctx.get("last_stop_poll", 0.0) < STOP_POLL_SEC:
        return False
    ctx["last_stop_poll"] = now
    return stop.wants_now(ctx["cfg"])


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
    code, status = stream_round(ctx)
    interrupted = status == "interrupted"
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
        "stopped": status == "stopped",
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


def engine_error_limit(ctx):
    """單一壞單位會在 max_engine_errors 次後被標 failed；再多一輪還是引擎錯誤＝系統性問題，停下來。"""
    return ctx["cfg"].limits["max_engine_errors"] + 1


def consecutive_engine_errors(ctx):
    count = 0
    for record in reversed(ctx["rounds"]):
        if record["exit"] in QUALITY_EXITS:
            break
        count += 1
    return count


def raise_alert(ctx):
    """引擎錯誤連發：先記進本次的 ALERT 原因，結束時統一寫 ALERT.txt。"""
    limit = engine_error_limit(ctx)
    lines = [f"連續 {limit} 輪引擎錯誤（結束碼不是 0／1），已停止連續執行；最後幾輪："]
    lines += [
        f"  第 {r['no']} 輪｜{r['unit']}｜exit={r['exit']}｜heartbeat：{r['heartbeat']}" for r in ctx["rounds"][-limit:]
    ]
    ctx["alert_lines"] += lines


def failed_units(state):
    """queue 中所有 failed／rejected 的單位（含以前就失敗、還沒人 reset 的）。"""
    return [
        (unit, unit_rec(state, unit).get("status"))
        for unit in (state or {}).get("queue", [])
        if unit_rec(state, unit).get("status") in ("failed", "rejected")
    ]


def finalize_alerts(ctx, code):
    """結束時：有任何 ALERT 原因或 failed／rejected 單位 → 寫 ALERT.txt 並回傳非 0 結束碼。"""
    state = safe_state(ctx["cfg"])
    units = failed_units(state)
    if units:
        ctx["alert_lines"].append("需要人看過的單位（看完後 reset <id> 才會重做）：")
        ctx["alert_lines"] += [f"  {unit}：{status}" for unit, status in units]
    if units and code == EXIT_OK:
        code = EXIT_UNIT_FAILED
    if not ctx["alert_lines"]:
        return code
    path = run_path(ctx["cfg"], "ALERT.txt")
    lines = [f"時間：{datetime.now():%Y-%m-%d %H:%M:%S}", f"log：{ctx['log_path']}", *ctx["alert_lines"]]
    lines = [notify.redact(line) for line in lines]  # ALERT.txt 進版控：通知金鑰一律遮蔽
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        say(ctx, f"（寫 ALERT.txt 失敗：{exc}）")
    notify.send(
        ctx["cfg"],
        "alert",
        title="ALERT（連續執行）",
        unit="、".join(unit for unit, _ in units) or "-",
        reason="\n".join(ctx["alert_lines"]),
        summary=f"詳見 {path}",
    )
    bar = "!" * 60
    say(ctx, f"\n{bar}\n!! 警告：詳見 {path}\n{bar}")
    for line in lines:
        say(ctx, "!! " + line)
    return code if code != EXIT_OK else EXIT_UNIT_FAILED


# ---------- 主迴圈 ----------


def stop_reason(args, ctx):
    if stop.requested(ctx["cfg"]):
        return STOP_REQUESTED
    if time.monotonic() - ctx["start"] >= ctx["limit_sec"]:
        return f"時間到（{args.hours} 小時）"
    if args.max_rounds is not None and len(ctx["rounds"]) >= args.max_rounds:
        return f"已達 --max-rounds {args.max_rounds}"
    state = safe_state(ctx["cfg"])
    if state is None:
        return STATE_UNREADABLE
    if not runnable_units(ctx["cfg"], state):
        return "沒有可執行的工作單位"
    return None


def wait_gap(args, ctx):
    """回傳 None＝等完；"ctrl-c"＝等待中被 Ctrl+C；"stop"＝等待中有人執行 stop。每秒看一次停止要求。"""
    if args.gap_seconds <= 0:
        return None
    say(ctx, f"（等 {args.gap_seconds} 秒再開下一輪）")
    deadline = time.monotonic() + args.gap_seconds
    try:
        while time.monotonic() < deadline:
            if stop.requested(ctx["cfg"]):
                say(ctx, "\n⛔ 等待中收到停止要求")
                return "stop"
            time.sleep(min(STOP_POLL_SEC, max(0.0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        say(ctx, "\n⛔ 等待中收到 Ctrl+C")
        return "ctrl-c"
    return None


def control_change(ctx):
    """與開跑時的基準比對工作目錄控制檔；有變動回傳說明。"""
    after = integrity.control_snapshot(ctx["cfg"])
    changed = integrity.diff(ctx["control_base"], after)
    return integrity.describe_control(ctx["control_base"], after, changed) if changed else ""


def apply_queue_between_rounds(cfg):
    """hours 持鎖、這時沒有 run 子程序：先套用佇列（例如人剛核准的單位），再判斷還有沒有可執行的單位。"""
    from lhf import round as round_mod

    try:
        round_mod.apply_pending_commands(cfg, load_state(cfg))
    except RoundError as exc:
        print(f"（套用佇列指令失敗：{exc}）", file=sys.stderr)


def before_round(args, ctx):
    """開新的一輪前的檢查；回傳 (停止原因, 結束碼) 或 None＝可以開跑。"""
    apply_queue_between_rounds(ctx["cfg"])
    reason = stop_reason(args, ctx)
    if reason == STATE_UNREADABLE:
        ctx["alert_lines"].append(f"{STATE_UNREADABLE}：引擎不會覆寫它，請人工檢查 run/state.json")
        return reason, EXIT_CONFIG
    if reason:
        return reason, EXIT_INTERRUPTED if reason == STOP_REQUESTED else EXIT_OK
    changed = control_change(ctx)
    if changed:
        ctx["alert_lines"].append(f"輪與輪之間{changed}；已停止連續執行")
        return "工作目錄控制檔被改動", EXIT_CONTROL_CHANGED
    integrity.clear_foreign_marker(ctx["cfg"])  # 上一輪的 run 子程序已結束：留下的標記都是殘留
    return None


def after_round(args, ctx, record, interrupted):
    """一輪結束後的檢查；回傳 (停止原因, 結束碼) 或 None＝繼續。"""
    if interrupted:
        return "Ctrl+C 中斷", EXIT_INTERRUPTED
    if record.get("stopped") or stop.requested(ctx["cfg"]):
        return STOP_REQUESTED, EXIT_INTERRUPTED
    if record["exit"] == EXIT_CONTROL_CHANGED:
        ctx["alert_lines"].append(
            f"第 {record['no']} 輪（{record['unit']}）指令站期間工作目錄控制檔被改動；已停止連續執行"
        )
        return "工作目錄控制檔被改動", EXIT_CONTROL_CHANGED
    if consecutive_engine_errors(ctx) >= engine_error_limit(ctx):
        raise_alert(ctx)
        return f"連續 {engine_error_limit(ctx)} 輪引擎錯誤", EXIT_CONFIG
    if stop_reason(args, ctx) is None:
        waited = wait_gap(args, ctx)
        if waited == "ctrl-c":
            return "Ctrl+C 中斷（等待中）", EXIT_INTERRUPTED
        if waited == "stop":
            return STOP_REQUESTED, EXIT_INTERRUPTED
    return None


def run_loop(args, ctx):
    while True:
        result = before_round(args, ctx)
        if result:
            return result
        record, interrupted = run_one_round(ctx)
        result = after_round(args, ctx, record, interrupted)
        if result:
            return result


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


def short_summary(ctx, reason, code):
    """給 Telegram 的簡短摘要（完整版在 hours-summary-*.md）。"""
    state = safe_state(ctx["cfg"]) or {}
    done, total = done_counts(state)
    units = touched_units(ctx)
    cost = sum(float(unit_rec(state, unit).get("cost_usd") or 0.0) for unit in units)
    lines = [
        f"停止原因：{reason}（exit {code}）",
        f"總時長：{fmt_hm(time.monotonic() - ctx['start'])}（HH:MM）｜輪數：{len(ctx['rounds'])}",
        f"完成單位：{done}/{total}｜等值成本 US$ {cost:.2f}",
    ]
    lines += [f"  {r['no']}. {r['unit']} → {r['status']}（exit {r['exit']}）" for r in ctx["rounds"][-20:]]
    failed = failed_units(state)
    if failed:
        lines.append("需要人看：" + "、".join(f"{unit} {status}" for unit, status in failed))
    return "\n".join(lines)


def write_summary(ctx, reason, code):
    path = run_path(ctx["cfg"], "logs") / f"hours-summary-{ctx['stamp']}.md"
    try:
        path.write_text(summary_markdown(ctx, reason, code), encoding="utf-8")
    except OSError as exc:
        say(ctx, f"（寫摘要失敗：{exc}）")
    else:
        say(ctx, f"\n{'=' * 60}\n停止：{reason}｜共 {len(ctx['rounds'])} 輪｜摘要：{path}\n{'=' * 60}")
    notify.send(ctx["cfg"], "finish", title="連續執行結束", summary=short_summary(ctx, reason, code))


# ---------- 進入點 ----------


def archive_trace(cfg):
    """trace.jsonl 太大就整檔改名歸檔（gate 預算本來就只讀本次呼叫之後的部分，歸檔不影響判斷）。"""
    path = trace_path(cfg)
    if file_offset(path) < cfg.limits["trace_archive_mb"] * 1024 * 1024:
        return None
    target = path.with_name(f"trace-{stamp()}.jsonl")
    atomic_replace(path, target)
    return target


def cmd_hours(cfg, args):
    with lock.holding(cfg, "hours") as info:
        stop.clear_request(cfg, "清除上次留下的停止要求")
        integrity.clear_foreign_marker(cfg)
        archived = archive_trace(cfg)
        if archived:
            print(f"（trace.jsonl 已歸檔為 {archived.name}）")
        try:
            return run_hours(cfg, args, info["token"])  # 每輪的 run 子程序憑這個 token 沿用同一把鎖
        finally:
            stop.clear_request(cfg)


def run_hours(cfg, args, token):
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
            "alert_lines": [],
            "token": token,
            "control_base": integrity.control_snapshot(cfg),  # 工作目錄控制檔基準：之後每輪開始前比對
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
            code = finalize_alerts(ctx, code)
            write_summary(ctx, reason, code)
        return code
