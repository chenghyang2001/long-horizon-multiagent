"""控制閘門：claude -p 每次工具呼叫前（PreToolUse）與呼叫後（PostToolUse）都執行本程式。

PreToolUse 判斷順序：全域禁止 → 意圖合約（工具）→ 路徑合約（Write／Edit）→ 需核准指令
                    → Bash 白名單 → 角色預算 → allow。結果寫一行 kind=decision 到 run/trace.jsonl。
需核准指令（approval_commands 命中且尚未核准）：deny，並寫 run/approvals/pending.jsonl，
引擎本站結束後把單位標 pending_approval，人執行 approve 後重跑本站。
PostToolUse（--post）只記錄工具實際結果（kind=result）。
任何內部錯誤一律 fail closed（deny），且永遠 exit 0，避免 hook 失敗被當成放行。

專案位置：--project <dir>，沒給就用環境變數 LH_PROJECT_DIR。
角色／單位／輪次／站：LH_ROLE（缺就用 hook 輸入的 agent_type，再缺＝unknown）、LH_UNIT、LH_ROUND、LH_STATION。

用法：
    python gate.py --project DIR              PreToolUse hook（stdin 讀 hook JSON）
    python gate.py --project DIR --post       PostToolUse hook
    python gate.py --project DIR --report [--unit X]
    python gate.py --project DIR --reset-trace
"""

import argparse
import fnmatch
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):  # 被 hook 以檔案路徑執行時，把 engine/ 加進 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lhf.config import ID_RE, load_config, norm_text, render_path  # noqa: E402
from lhf.errors import MissingVar  # noqa: E402
from lhf.state import append_jsonl, approved_path, pending_approvals_path, read_jsonl, trace_path  # noqa: E402

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PREVIEW_LEN = 120
SHELL_META = set(";&|<>`$") | {chr(10), chr(13)}  # 換行也算：可在後面接第二條指令


def setup_utf8_io():
    """hook 不一定帶 PYTHONUTF8=1，cp950 會讓中文原因變亂碼。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def run_context(event):
    """單位 id 不合格（例如含 ../）就當成沒有單位：需要單位的路徑合約一律不放行。"""
    unit = os.environ.get("LH_UNIT", "")
    return {
        "role": os.environ.get("LH_ROLE") or event.get("agent_type") or "unknown",
        "round": os.environ.get("LH_ROUND", ""),
        "unit": unit if ID_RE.match(unit) else "",
        "station": os.environ.get("LH_STATION", ""),
        "session": event.get("session_id", ""),
    }  # 預算以每次 claude -p 計，重做時有新額度


# ---------- 路徑 ----------


def norm_path(raw_path, cwd):
    """把工具給的路徑轉成正規化的絕對路徑字串；相對路徑以 hook 的 cwd 為基準。"""
    candidate = Path(str(raw_path).replace("\\", "/"))
    if not candidate.is_absolute():
        candidate = Path(str(cwd)) / candidate
    return norm_text(candidate.resolve().as_posix())


def expand_pattern(cfg, pattern, ctx):
    """展開佔位符；用到單位相關變數卻沒有 LH_UNIT（或變數未定義）就回 None（不放行）。"""
    try:
        mapping = cfg.vars(unit=ctx["unit"] or None, round_no=ctx["round"] or None)
        return norm_text(render_path(pattern, mapping))
    except MissingVar:
        return None


def path_allowed(cfg, target, patterns, ctx):
    """fnmatch 的 * 本來就會跨越 /，所以 ** 自然等於任意層。"""
    for pattern in patterns or []:
        expanded = expand_pattern(cfg, pattern, ctx)
        if expanded and fnmatch.fnmatchcase(target, expanded):
            return True
    return False


def write_target(cfg, event):
    if event.get("tool_name") not in WRITE_TOOLS:
        return None
    tool_input = event.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        raise ValueError("寫入工具缺少 file_path")
    return norm_path(raw, event.get("cwd") or cfg.work_dir())


def bash_command(event):
    if event.get("tool_name") != "Bash":
        return None
    return str((event.get("tool_input") or {}).get("command", ""))


# ---------- 各項檢查（回傳 deny 原因或 None） ----------


def norm_command(command):
    """比對用的指令正規化：小寫、反斜線轉 /、去掉引號、連續空白壓成一格。
    擋得住 GIT  PUSH、fab''le5、反斜線路徑這類變形；真正的安全邊界仍是 bash_allow 白名單。"""
    text = str(command).lower().replace("\\", "/").replace("'", "").replace('"', "")
    return " ".join(text.split())


def check_global(cfg, target, command):
    """forbidden.paths 同時套用到寫入目標與 Bash 指令字串（額外一層，不取代白名單）。"""
    forbidden = cfg.data.get("forbidden") or {}
    normalized = norm_command(command) if command is not None else None
    for item in forbidden.get("paths") or []:
        if target and norm_text(item) in target:
            return f"全域禁止寫入 {item}"
        if normalized is not None and norm_command(item) in normalized:
            return f"全域禁止指令：碰到受保護路徑 {item}"
    for item in forbidden.get("commands") or []:
        if normalized is not None and norm_command(item) in normalized:
            return f"全域禁止指令：含「{item}」"
    return None


def check_contract(cfg, event, role_policy, ctx, target):
    tool = event.get("tool_name")
    if tool not in (role_policy.get("tools") or []):
        return f"{ctx['role']} 的合約不允許 {tool}"
    if target is not None and not path_allowed(cfg, target, role_policy.get("write"), ctx):
        return f"{ctx['role']} 不可寫入 {target}（不在 write，或單位不符）"
    return None


def needs_approval(cfg, command, ctx):
    """命中 approval_commands 且這個單位還沒核准過完全相同的指令 → 需要人工核准。"""
    if command is None:
        return False
    if not any(re.search(pattern, command) for pattern in cfg.data.get("approval_commands") or []):
        return False
    approved = read_jsonl(approved_path(cfg))
    return not any(r.get("unit") == ctx["unit"] and r.get("command") == command for r in approved)


def check_bash(role_policy, role, command):
    """白名單之外再擋 shell 控制字元：白名單只看開頭，; | & > $ 等可以在後面接任意指令。"""
    patterns = role_policy.get("bash_allow") or []
    if not patterns:
        return f"{role} 沒有 bash_allow，不可執行 Bash"
    meta = sorted({ch for ch in command if ch in SHELL_META})
    if meta:
        return f"Bash 指令不可含 shell 控制字元 {meta}（串接、重導、變數展開一律不放行）"
    if any(re.search(pattern, command) for pattern in patterns):
        return None
    return f"{role} 的 bash_allow 不允許此指令"


def count_allowed(cfg, ctx):
    """預算只算同一輪、同一單位、同一角色、同一 session 真的放行過的工具呼叫。"""
    return sum(
        1
        for rec in read_jsonl(trace_path(cfg))
        if rec.get("kind") == "decision"
        and rec.get("decision") == "allow"
        and rec.get("role") == ctx["role"]
        and rec.get("round") == ctx["round"]
        and rec.get("unit") == ctx["unit"]
        and rec.get("session", "") == ctx["session"]
    )


def check_budget(cfg, ctx):
    limit = cfg.roles[ctx["role"]].get("budget")
    if limit is not None and count_allowed(cfg, ctx) >= int(limit):
        return f"預算用完：{ctx['role']} 每次呼叫最多 {limit} 次工具呼叫"
    return None


def decide(event, cfg, ctx):
    """回傳 (decision, reason, 是否需要核准)。"""
    if not event.get("tool_name"):
        raise ValueError("hook 輸入缺少 tool_name")
    target, command = write_target(cfg, event), bash_command(event)
    reason = check_global(cfg, target, command)
    if reason:
        return "deny", reason, False
    role_policy = cfg.roles.get(ctx["role"])
    if not isinstance(role_policy, dict):
        return "deny", f"未知角色 {ctx['role']}（沒有 LH_ROLE 也沒有 agent_type，或 roles 沒定義）", False
    reason = check_contract(cfg, event, role_policy, ctx, target)
    if reason:
        return "deny", reason, False
    if needs_approval(cfg, command, ctx):
        return "deny", "此指令需要人工核准（已寫入 approvals/pending.jsonl，核准後重跑本站）", True
    reason = (check_bash(role_policy, ctx["role"], command) if command is not None else None) or check_budget(cfg, ctx)
    if reason:
        return "deny", reason, False
    return "allow", "通過全域禁止、合約、路徑、核准與預算檢查", False


# ---------- 紀錄與輸出 ----------


def preview(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text[:PREVIEW_LEN]


def trace_record(ctx, tool, kind, decision, reason, tool_input):
    return {
        "ts": now_iso(),
        "round": ctx.get("round", ""),
        "unit": ctx.get("unit", ""),
        "session": ctx.get("session", ""),
        "role": ctx.get("role", "unknown"),
        "tool": tool,
        "kind": kind,
        "decision": decision,
        "reason": reason,
        "input": preview(tool_input or {}),
    }


def record_pending(cfg, ctx, command):
    append_jsonl(
        pending_approvals_path(cfg),
        {
            "ts": now_iso(),
            "unit": ctx["unit"],
            "round": ctx["round"],
            "role": ctx["role"],
            "station": ctx["station"],
            "kind": "command",
            "command": command,
        },
    )


def emit(permission, reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": permission,
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )


def read_event():
    event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    if not isinstance(event, dict):
        raise TypeError("hook 輸入不是 JSON 物件")
    return event


def fallback_trace_path(project_dir):
    """設定檔壞掉時仍盡量把 deny 記到 <project>/run/trace.jsonl。"""
    return Path(project_dir) / "run" / "trace.jsonl" if project_dir else None


def fail_closed(error, ctx, project_dir):
    """閘門自己壞掉時寧可擋下，否則壞掉就等於全部放行。"""
    reason = f"gate 內部錯誤，預設拒絕：{type(error).__name__}: {error}"
    print(reason, file=sys.stderr)
    emit("deny", reason)
    path = fallback_trace_path(project_dir)
    if path is None:
        return
    try:
        append_jsonl(path, trace_record(ctx or run_context({}), "unknown", "decision", "deny", reason, {}))
    except OSError:
        pass


def run_hook(project_dir):
    ctx = None
    try:
        event = read_event()
        ctx = run_context(event)
        if not project_dir:
            raise ValueError("沒有 --project 也沒有 LH_PROJECT_DIR")
        cfg = load_config(project_dir)
        decision, reason, approval = decide(event, cfg, ctx)
        if approval:
            record_pending(cfg, ctx, bash_command(event))
        append_jsonl(
            trace_path(cfg),
            trace_record(ctx, event.get("tool_name"), "decision", decision, reason, event.get("tool_input")),
        )
        emit(decision, reason)
    except Exception as error:  # noqa: BLE001 — 閘門必須攔下所有錯誤才能 fail closed
        fail_closed(error, ctx, project_dir)
    return 0


def judge_response(response):
    if isinstance(response, dict):
        failed = bool(response.get("is_error") or response.get("error") or response.get("interrupted"))
        return ("failure" if failed else "success"), preview(response)
    text = "" if response is None else str(response)
    return ("failure" if "error" in text.lower() else "success"), preview(text)


def run_post(project_dir):
    try:
        event = read_event()
        cfg = load_config(project_dir)
        outcome, output = judge_response(event.get("tool_response"))
        append_jsonl(
            trace_path(cfg),
            trace_record(
                run_context(event), event.get("tool_name"), "result", outcome, output, event.get("tool_input")
            ),
        )
    except Exception as error:  # noqa: BLE001 — PostToolUse 失敗不能讓 claude -p 中斷
        print(f"gate --post 記錄失敗：{type(error).__name__}: {error}", file=sys.stderr)
    return 0


# ---------- 報表 ----------


def print_stats(records):
    stats = Counter((rec.get("role") or "?", rec.get("decision")) for rec in records)
    print("\n統計：")
    for role in sorted({rec.get("role") or "?" for rec in records}):
        print(
            f"  {role:<12} allow={stats[(role, 'allow')]}  deny={stats[(role, 'deny')]}  "
            f"成功={stats[(role, 'success')]}  失敗={stats[(role, 'failure')]}"
        )


def cmd_report(cfg, unit):
    records = [r for r in read_jsonl(trace_path(cfg)) if not unit or r.get("unit") == unit]
    if not records:
        print("沒有追蹤紀錄。")
        return
    print(f"{'時間':<19} | {'輪':<3} | {'單位':<8} | {'角色':<10} | {'工具':<6} | {'種類':<8} | {'決策':<6} | 原因")
    print("-" * 110)
    for rec in records:
        print(
            f"{rec.get('ts', ''):<21} | {rec.get('round', '')!s:<4} | {rec.get('unit', ''):<10} | "
            f"{rec.get('role', ''):<12} | {rec.get('tool', ''):<8} | {rec.get('kind', ''):<10} | "
            f"{rec.get('decision', ''):<8} | {rec.get('reason', '')}"
        )
    print_stats(records)


def cmd_reset_trace(cfg):
    path = trace_path(cfg)
    if path.exists():
        path.unlink()
        print(f"已刪除：{path}")
    else:
        print("trace.jsonl 不存在，不需刪除。")


def main(argv=None):
    """沒有 --report／--reset-trace 就當 hook 執行。永遠 exit 0。"""
    setup_utf8_io()
    parser = argparse.ArgumentParser(description="長時程工廠控制閘門")
    parser.add_argument("--project", help="含 factory.yaml 的專案資料夾（預設 LH_PROJECT_DIR）")
    parser.add_argument("--post", action="store_true", help="PostToolUse 模式：記錄工具結果")
    parser.add_argument("--report", action="store_true", help="印出追蹤紀錄表格")
    parser.add_argument("--unit", help="--report 時只看這個單位")
    parser.add_argument("--reset-trace", action="store_true", help="刪除 trace.jsonl")
    try:
        args, _unknown = parser.parse_known_args(argv)  # hook 不可因多餘參數而失敗（失敗＝放行風險）
    except SystemExit as exc:
        fail_closed(ValueError(f"參數解析失敗（exit {exc.code}）"), None, os.environ.get("LH_PROJECT_DIR", ""))
        return 0
    project_dir = args.project or os.environ.get("LH_PROJECT_DIR", "")
    if args.post:
        return run_post(project_dir)
    if not (args.report or args.reset_trace):
        return run_hook(project_dir)
    try:
        cfg = load_config(project_dir)
        cmd_report(cfg, args.unit) if args.report else cmd_reset_trace(cfg)
    except Exception as error:  # noqa: BLE001 — 報表工具出錯只印訊息
        print(f"錯誤：{error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
