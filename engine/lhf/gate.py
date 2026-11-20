"""控制閘門：claude -p 每次工具呼叫前（PreToolUse）與呼叫後（PostToolUse）都執行本程式。

PreToolUse 判斷順序：全域禁止 → 引擎內部檔保護 → 意圖合約（工具）→ 路徑合約（Write／Edit）
                    → Bash 白名單（含控制字元檢查）→ 需核准指令 → 角色預算 → allow。
                    結果寫一行 kind=decision 到 run/trace.jsonl。
需核准指令（approval_commands 命中且本輪沒有可用的核准）：deny，並寫 run/approvals/pending.jsonl，
引擎本站結束後把單位標 pending_approval，人執行 approve 後重跑本站。
核准綁定「單位＋輪次＋完全相同的指令」，放行一次就用掉（寫 approvals/used.jsonl）。
設定一律讀 run/.runtime/factory.snapshot.json（本輪開始時的快照，含 sha256）；
代理就算改了 factory.yaml 也影響不到本輪的 gate。
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
from pathlib import Path

if __package__ in (None, ""):  # 被 hook 以檔案路徑執行時，把 engine/ 加進 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lhf.common import now_iso  # noqa: E402
from lhf.config import ENGINE_DIR, ID_RE, load_config, norm_text, render_path  # noqa: E402
from lhf.errors import MissingVar  # noqa: E402
from lhf.snapshot import load_for_gate  # noqa: E402
from lhf.state import (  # noqa: E402
    append_jsonl,
    approvals_used_path,
    approved_path,
    pending_approvals_path,
    read_jsonl,
    read_jsonl_since,
    trace_path,
)

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


def run_context(event):
    """單位 id 不合格（例如含 ../）就當成沒有單位：需要單位的路徑合約一律不放行。"""
    unit = os.environ.get("LH_UNIT", "")
    return {
        "role": os.environ.get("LH_ROLE") or event.get("agent_type") or "unknown",
        "round": os.environ.get("LH_ROUND", ""),
        "unit": unit if ID_RE.match(unit) else "",
        "station": os.environ.get("LH_STATION", ""),
        "session": event.get("session_id", ""),
        "trace_offset": trace_offset_env(),
    }  # 預算以每次 claude -p 計，重做時有新額度


def trace_offset_env():
    """引擎在每次 claude -p 前記下 trace 的位元組位置；預算只讀這之後的紀錄，trace 再大也不會變慢。"""
    try:
        return max(0, int(os.environ.get("LH_TRACE_OFFSET", "0")))
    except ValueError:
        return 0


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


# 代理不可呼叫引擎本身（例如自己執行 approve 來核准自己）。比對對象是 norm_command 之後的字串
# （小寫、反斜線轉 /、去引號、空白壓成一格），所以 -mlhf、-Im lhf、l''hf、全形空白都會落到同一個樣子。
ENGINE_COMMAND_PATTERNS = (
    (re.compile(r"(^|[\s;&|(])-\w*m\s*lhf\b"), "python -m lhf"),  # -m lhf、-mlhf、-Im lhf、-m lhf.gate
    (re.compile(r"\bimport\s+lhf\b"), "import lhf"),
    (re.compile(r"\bfrom\s+lhf\b"), "from lhf"),
    (re.compile(r"\blhf[/.]"), "lhf/ 或 lhf."),  # 引擎檔案路徑或模組名
    (re.compile(r"(runpy|importlib|__import__)"), "動態匯入"),  # 用字串拼出模組名繞過；-mrunpy 前面沒有字界
)


def check_engine_command(normalized):
    for pattern, label in ENGINE_COMMAND_PATTERNS:
        if pattern.search(normalized):
            return f"不可在代理中呼叫引擎（指令含「{label}」）：核准、重設等只能由人執行"
    if norm_text(ENGINE_DIR.as_posix()) in normalized:
        return "不可在代理中呼叫引擎（指令含引擎目錄路徑）：核准、重設等只能由人執行"
    return None


def check_global(cfg, target, command):
    """forbidden.paths 同時套用到寫入目標與 Bash 指令字串（額外一層，不取代白名單）。"""
    forbidden = cfg.data.get("forbidden") or {}
    normalized = norm_command(command) if command is not None else None
    if normalized is not None and (reason := check_engine_command(normalized)):
        return reason
    for item in forbidden.get("paths") or []:
        if target and norm_text(item) in target:
            return f"全域禁止寫入 {item}"
        if normalized is not None and norm_command(item) in normalized:
            return f"全域禁止指令：碰到受保護路徑 {item}"
    for item in forbidden.get("commands") or []:
        if normalized is not None and norm_command(item) in normalized:
            return f"全域禁止指令：含「{item}」"
    return None


def check_protected(cfg, target):
    """引擎內部檔（state、trace、核准、設定、引擎程式）任何角色都不可寫，不看 roles 設定。"""
    if target is None:
        return None
    for pattern in cfg.protected_patterns():
        if fnmatch.fnmatchcase(target, pattern):
            return f"引擎內部檔不可寫入：{target}"
    return None


def check_contract(cfg, event, role_policy, ctx, target):
    tool = event.get("tool_name")
    if tool not in (role_policy.get("tools") or []):
        return f"{ctx['role']} 的合約不允許 {tool}"
    if target is not None and not path_allowed(cfg, target, role_policy.get("write"), ctx):
        return f"{ctx['role']} 不可寫入 {target}（不在 write，或單位不符）"
    if tool == "WebFetch":
        return check_fetch(role_policy, ctx["role"], (event.get("tool_input") or {}).get("url"))
    return None


def check_fetch(role_policy, role, url):
    """WebFetch 只放行 fetch_allow 白名單裡的網址；沒有網址或不符合一律拒絕。"""
    if not isinstance(url, str) or not url:
        return "WebFetch 缺少 url"
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return "WebFetch 的 url 含空白、換行或控制字元，一律拒絕"
    if any(re.search(pattern, url) for pattern in role_policy.get("fetch_allow") or []):
        return None
    return f"{role} 的 fetch_allow 不允許這個網址：{url[:120]}"


def approval_key(record):
    return (record.get("unit"), str(record.get("round")), record.get("command"))


def available_approval(cfg, command, ctx):
    """同單位、同輪次、完全相同的指令，核准次數多於已使用次數才算還有一次可用。"""
    key = (ctx["unit"], str(ctx["round"]), command)
    granted = sum(1 for r in read_jsonl(approved_path(cfg)) if approval_key(r) == key)
    used = sum(1 for r in read_jsonl(approvals_used_path(cfg)) if approval_key(r) == key)
    return granted > used


def needs_approval(cfg, command, ctx):
    """命中 approval_commands 且本輪沒有可用的核准 → 需要人工核准。"""
    if command is None:
        return False
    if not any(re.search(pattern, command) for pattern in cfg.data.get("approval_commands") or []):
        return False
    return not available_approval(cfg, command, ctx)


def is_approval_command(cfg, command):
    return command is not None and any(re.search(p, command) for p in cfg.data.get("approval_commands") or [])


def consume_approval(cfg, ctx, command):
    append_jsonl(
        approvals_used_path(cfg), {"ts": now_iso(), "unit": ctx["unit"], "round": str(ctx["round"]), "command": command}
    )


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
        for rec in read_jsonl_since(trace_path(cfg), ctx.get("trace_offset", 0))
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
    reason = check_global(cfg, target, command) or check_protected(cfg, target)
    if reason:
        return "deny", reason, False
    role_policy = cfg.roles.get(ctx["role"])
    if not isinstance(role_policy, dict):
        return "deny", f"未知角色 {ctx['role']}（沒有 LH_ROLE 也沒有 agent_type，或 roles 沒定義）", False
    reason = check_contract(cfg, event, role_policy, ctx, target)
    if not reason and command is not None:
        reason = check_bash(role_policy, ctx["role"], command)  # 需核准的指令也必須先符合白名單
    if reason:
        return "deny", reason, False
    if needs_approval(cfg, command, ctx):
        return "deny", "此指令需要人工核准（已寫入 approvals/pending.jsonl，核准後重跑本站）", True
    reason = check_budget(cfg, ctx)
    if reason:
        return "deny", reason, False
    return "allow", "通過全域禁止、內部檔保護、合約、路徑、白名單、核准與預算檢查", False


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
        cfg = load_for_gate(project_dir)
        decision, reason, approval = decide(event, cfg, ctx)
        if approval:
            record_pending(cfg, ctx, bash_command(event))
        elif decision == "allow" and is_approval_command(cfg, bash_command(event)):
            consume_approval(cfg, ctx, bash_command(event))
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
        cfg = load_for_gate(project_dir)
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
