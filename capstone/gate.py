"""畢業專案控制閘門：claude -p 每次工具呼叫前（PreToolUse）與呼叫後（PostToolUse）都執行本程式。

PreToolUse 依序檢查：全域禁止 → 意圖合約（工具）→ 路徑合約 → Bash 白名單 → 每輪預算，
輸出 allow／deny 並寫一行 kind=decision 到 trace.jsonl。
PostToolUse（--post）只記錄工具實際結果（kind=result），不做決策。
任何內部錯誤一律 fail closed（deny），且永遠 exit 0，避免 hook 失敗被 Claude Code 當成放行。

用法：
    python gate.py                      # PreToolUse hook，stdin 讀 hook JSON
    python gate.py --post               # PostToolUse hook
    python gate.py --report [--chapter ch04]
    python gate.py --reset-trace
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

import yaml

CAPSTONE = Path(__file__).resolve().parent
WORKTREE = Path.home() / "workspace" / "kindle-143-fable5-agentic-ai-factory"
PRACTICE = WORKTREE / "practice"
POLICY_PATH = CAPSTONE / "policy.yaml"
TRACE_PATH = CAPSTONE / "trace.jsonl"

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PREVIEW_LEN = 120


def setup_utf8_io():
    """stdout/stderr 改 UTF-8；hook 不一定帶 PYTHONUTF8=1，cp950 會讓中文原因變亂碼。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def now_iso():
    """回傳到秒的本地時間字串。"""
    return datetime.now().isoformat(timespec="seconds")


def load_policy():
    """讀 policy.yaml；格式不對就丟例外，交給上層 fail closed。"""
    with open(POLICY_PATH, encoding="utf-8") as policy_file:
        policy = yaml.safe_load(policy_file)
    if not isinstance(policy, dict) or not isinstance(policy.get("roles"), dict):
        raise TypeError("policy.yaml 缺少 roles 區段")
    return policy


def run_context(event):
    """從環境變數與 hook 輸入取出 角色／輪次／章節；角色缺就是 unknown。"""
    role = os.environ.get("LH_ROLE") or event.get("agent_type") or "unknown"
    return {
        "role": role,
        "round": os.environ.get("LH_ROUND", ""),
        "chapter": os.environ.get("LH_CHAPTER", ""),
        "session": event.get("session_id", ""),  # 預算以每次 claude -p 計，重做時有新額度
    }


def norm_text(text):
    """比對前統一：\\ 轉 /、轉小寫，讓 C:\\USERS 與 c:/users 視為同一路徑。"""
    return str(text).replace("\\", "/").lower()


def norm_path(raw_path, cwd):
    """把工具給的路徑轉成正規化的絕對路徑字串；相對路徑以 hook 的 cwd 為基準。"""
    candidate = Path(str(raw_path).replace("\\", "/"))
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    return norm_text(candidate.resolve().as_posix())


def expand_pattern(pattern, chapter):
    """展開 {CAPSTONE}／{PRACTICE}／{chapter}；需要章節卻沒有 LH_CHAPTER 就回 None（不放行）。"""
    if "{chapter}" in pattern and not chapter:
        return None
    expanded = (pattern.replace("{CAPSTONE}", CAPSTONE.as_posix())
                .replace("{PRACTICE}", PRACTICE.resolve().as_posix())
                .replace("{chapter}", chapter))
    return norm_text(expanded)


def path_allowed(target, patterns, chapter):
    """fnmatch 的 * 本來就會跨越 /，所以 ** 自然等於任意層。"""
    for pattern in patterns or []:
        expanded = expand_pattern(pattern, chapter)
        if expanded and fnmatch.fnmatchcase(target, expanded):
            return True
    return False


def write_target(event):
    """Write／Edit 類工具的目標路徑（已正規化）；不是寫入工具就回 None。"""
    if event.get("tool_name") not in WRITE_TOOLS:
        return None
    tool_input = event.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        raise ValueError("寫入工具缺少 file_path")
    return norm_path(raw, event.get("cwd") or PRACTICE)


def bash_command(event):
    """Bash 工具的指令字串；不是 Bash 就回 None。"""
    if event.get("tool_name") != "Bash":
        return None
    return str((event.get("tool_input") or {}).get("command", ""))


def check_global(policy, target, command):
    """全域禁止：任何角色都不能寫入書附原始碼、不能跑危險指令。"""
    for forbidden in policy.get("forbidden_paths") or []:
        if target and norm_text(forbidden) in target:
            return f"全域禁止寫入 {forbidden}（書附原始碼不可改）"
    for forbidden in policy.get("forbidden_commands") or []:
        if command is not None and forbidden.lower() in command.lower():
            return f"全域禁止指令：含「{forbidden}」"
    return None


def check_bash(role_policy, role, command):
    """只有 policy 寫了 bash_allow 的角色、且指令符合其中一條 regex 才放行。"""
    patterns = role_policy.get("bash_allow") or []
    if not patterns:
        return f"{role} 沒有 bash_allow，不可執行 Bash"
    if any(re.search(pattern, command) for pattern in patterns):
        return None
    return f"{role} 的 bash_allow 不允許此指令"


def count_allowed(ctx):
    """預算只算同一輪、同一章、同一角色真的放行過的工具呼叫。"""
    if not TRACE_PATH.exists():
        return 0
    count = 0
    with open(TRACE_PATH, encoding="utf-8") as trace_file:
        for line in trace_file:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (rec.get("kind") == "decision" and rec.get("decision") == "allow"
                    and rec.get("role") == ctx["role"] and rec.get("round") == ctx["round"]
                    and rec.get("chapter") == ctx["chapter"] and rec.get("session", "") == ctx.get("session", "")):
                count += 1
    return count


def check_budget(policy, ctx):
    """超過該角色每輪工具呼叫上限就擋下；沒設上限視為不限。"""
    limit = (policy.get("budgets") or {}).get(ctx["role"])
    if limit is None:
        return None
    if count_allowed(ctx) >= int(limit):
        return f"預算用完：{ctx['role']} 每輪最多 {limit} 次工具呼叫"
    return None


def check_contract(event, role_policy, ctx, target, command):
    """意圖合約（工具）→ 路徑合約 → Bash 白名單；回傳 deny 原因或 None。"""
    tool = event.get("tool_name")
    if tool not in (role_policy.get("tools") or []):
        return f"{ctx['role']} 的合約不允許 {tool}"
    if target is not None and not path_allowed(target, role_policy.get("write_paths"), ctx["chapter"]):
        return f"{ctx['role']} 不可寫入 {target}（不在 write_paths，或章節不符）"
    if command is not None:
        return check_bash(role_policy, ctx["role"], command)
    return None


def decide(event, policy, ctx):
    """依合約順序檢查，回傳 (decision, reason)。"""
    if not event.get("tool_name"):
        raise ValueError("hook 輸入缺少 tool_name")
    target = write_target(event)
    command = bash_command(event)
    reason = check_global(policy, target, command)
    if reason:
        return "deny", reason
    role_policy = policy["roles"].get(ctx["role"])
    if not isinstance(role_policy, dict):
        return "deny", f"未知角色 {ctx['role']}（沒有 LH_ROLE 也沒有 agent_type，或 policy 沒定義）"
    reason = check_contract(event, role_policy, ctx, target, command) or check_budget(policy, ctx)
    if reason:
        return "deny", reason
    return "allow", "通過全域禁止、合約、路徑與預算檢查"


def preview(value):
    """只留前 120 字，避免 Write 的整篇內容塞爆追蹤檔。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text[:PREVIEW_LEN]


def write_trace(ctx, tool, kind, decision, reason, tool_input):
    """每次決策或結果都留一行，事後才能還原誰在哪一輪做了什麼。"""
    record = {
        "ts": now_iso(), "round": ctx.get("round", ""), "chapter": ctx.get("chapter", ""), "session": ctx.get("session", ""),
        "role": ctx.get("role", "unknown"), "tool": tool, "kind": kind,
        "decision": decision, "reason": reason, "input": preview(tool_input or {}),
    }
    with open(TRACE_PATH, "a", encoding="utf-8") as trace_file:
        trace_file.write(json.dumps(record, ensure_ascii=False) + "\n")


def emit(permission, reason):
    """印出 Claude Code PreToolUse hook 認得的決策 JSON。"""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": permission,
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))


def read_event():
    """從 stdin 讀 hook JSON；不是物件就丟例外。"""
    event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    if not isinstance(event, dict):
        raise TypeError("hook 輸入不是 JSON 物件")
    return event


def fail_closed(error, ctx=None):
    """閘門自己壞掉時寧可擋下，否則壞掉就等於全部放行。"""
    reason = f"gate 內部錯誤，預設拒絕：{type(error).__name__}: {error}"
    print(reason, file=sys.stderr)
    emit("deny", reason)
    try:
        write_trace(ctx or run_context({}), "unknown", "decision", "deny", reason, {})
    except OSError:
        pass


def run_hook():
    """PreToolUse 模式：讀輸入 → 決策 → 寫 trace → 輸出；任何錯誤都 fail closed。"""
    ctx = None
    try:
        event = read_event()
        ctx = run_context(event)
        decision, reason = decide(event, load_policy(), ctx)
        write_trace(ctx, event.get("tool_name"), "decision", decision, reason, event.get("tool_input"))
        emit(decision, reason)
    except Exception as error:  # noqa: BLE001 — 閘門必須攔下所有錯誤才能 fail closed
        fail_closed(error, ctx)
    return 0


def judge_response(response):
    """盡量容錯地判斷工具結果成功與否：dict 看錯誤欄位，字串看有沒有 error。"""
    if isinstance(response, dict):
        failed = bool(response.get("is_error") or response.get("error") or response.get("interrupted"))
        return ("failure" if failed else "success"), preview(response)
    text = "" if response is None else str(response)
    return ("failure" if "error" in text.lower() else "success"), preview(text)


def run_post():
    """PostToolUse 模式：只記錄工具實際結果，不輸出決策；錯誤只寫 stderr。"""
    try:
        event = read_event()
        ctx = run_context(event)
        outcome, output = judge_response(event.get("tool_response"))
        write_trace(ctx, event.get("tool_name"), "result", outcome, output, event.get("tool_input"))
    except Exception as error:  # noqa: BLE001 — PostToolUse 失敗不能讓 claude -p 中斷
        print(f"gate --post 記錄失敗：{type(error).__name__}: {error}", file=sys.stderr)
    return 0


def read_trace(chapter):
    """讀 trace.jsonl，壞行略過；可只取某一章。"""
    if not TRACE_PATH.exists():
        return []
    records = []
    with open(TRACE_PATH, encoding="utf-8") as trace_file:
        for line in trace_file:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not chapter or rec.get("chapter") == chapter:
                records.append(rec)
    return records


def print_stats(records):
    """各角色的 allow／deny（決策）與 success／failure（結果）次數。"""
    stats = Counter((rec.get("role") or "?", rec.get("decision")) for rec in records)
    roles = sorted({rec.get("role") or "?" for rec in records})
    print("\n統計：")
    for role in roles:
        print(f"  {role:<12} allow={stats[(role, 'allow')]}  deny={stats[(role, 'deny')]}  "
              f"成功={stats[(role, 'success')]}  失敗={stats[(role, 'failure')]}")


def cmd_report(chapter):
    """把 trace 印成表格。"""
    records = read_trace(chapter)
    if not records:
        print("沒有追蹤紀錄。")
        return
    # 標頭含中文（全形佔兩格），資料欄寬比標頭多 2，終端機上才對得齊
    print(f"{'時間':<19} | {'輪':<3} | {'章':<4} | {'角色':<10} | {'工具':<6} | "
          f"{'種類':<6} | {'決策':<6} | 原因")
    print("-" * 110)
    for rec in records:
        print(f"{rec.get('ts', ''):<21} | {rec.get('round', '')!s:<4} | "
              f"{rec.get('chapter', ''):<5} | {rec.get('role', ''):<12} | {rec.get('tool', ''):<8} | "
              f"{rec.get('kind', ''):<8} | {rec.get('decision', ''):<8} | {rec.get('reason', '')}")
    print_stats(records)


def cmd_reset_trace():
    """刪除 trace.jsonl，重新開始記錄。"""
    if TRACE_PATH.exists():
        TRACE_PATH.unlink()
        print(f"已刪除：{TRACE_PATH.name}")
    else:
        print("trace.jsonl 不存在，不需刪除。")


def main():
    """解析參數；沒有參數就當 PreToolUse hook 執行。永遠 exit 0。"""
    setup_utf8_io()
    parser = argparse.ArgumentParser(description="畢業專案控制閘門")
    parser.add_argument("--post", action="store_true", help="PostToolUse 模式：記錄工具結果")
    parser.add_argument("--report", action="store_true", help="印出追蹤紀錄表格")
    parser.add_argument("--chapter", metavar="chNN", help="--report 時只看這一章")
    parser.add_argument("--reset-trace", action="store_true", help="刪除 trace.jsonl")
    args = parser.parse_args()
    try:
        if args.post:
            return run_post()
        if args.report:
            cmd_report(args.chapter)
        elif args.reset_trace:
            cmd_reset_trace()
        else:
            return run_hook()
    except (OSError, ValueError) as error:
        print(f"錯誤：{error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
