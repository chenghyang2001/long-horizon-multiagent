"""階段 3 控制閘門：Claude Code PreToolUse hook 每次工具呼叫前都執行本程式。

依序檢查 意圖合約 → 權限預算 → 人工核准，輸出 allow／deny，並把每次決策寫進 trace.jsonl。

用法：
    python gate.py                      # 當 hook 用，stdin 讀 hook JSON
    python gate.py --approve send_report
    python gate.py --report [--session ID]
    python gate.py --reset
"""
import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

BASE = Path(__file__).parent
POLICY_PATH = BASE / "policy.yaml"
TRACE_PATH = BASE / "trace.jsonl"
APPROVALS_PATH = BASE / "approvals.json"
PENDING_PATH = BASE / "pending-approvals.jsonl"
RESET_TARGETS = [TRACE_PATH, APPROVALS_PATH, PENDING_PATH, BASE / "outbox", BASE / "notes"]

SEND_REPORT_PREFIX = "python send_report.py"
INPUT_PREVIEW_LEN = 80


def setup_utf8_io():
    """把 stdout/stderr 改成 UTF-8；hook 不一定帶 PYTHONUTF8=1，cp950 會讓中文原因變亂碼或噴錯。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def now_iso():
    """回傳到秒的本地時間字串。"""
    return datetime.now().isoformat(timespec="seconds")


def load_policy():
    """讀 policy.yaml；讀不到或格式錯就丟例外，交給上層 fail closed。"""
    with open(POLICY_PATH, encoding="utf-8") as policy_file:
        policy = yaml.safe_load(policy_file)
    if not isinstance(policy, dict) or not isinstance(policy.get("roles"), dict):
        raise TypeError("policy.yaml 缺少 roles 區段")
    return policy


def resolve_actor(event):
    """子代理的呼叫才有 agent_type；沒有就是主持人自己。"""
    agent_type = event.get("agent_type")
    return agent_type if agent_type else "host"


def resolve_tool(event):
    """把「python send_report.py ...」的 Bash 呼叫視為虛擬工具 send_report，其他 Bash 仍是 Bash。"""
    tool_name = event.get("tool_name")
    if not tool_name:
        raise ValueError("hook 輸入缺少 tool_name")
    if tool_name == "Bash":
        command = str((event.get("tool_input") or {}).get("command", "")).strip()
        if command.startswith(SEND_REPORT_PREFIX):
            return "send_report"
    return tool_name


def summarize_input(tool_input):
    """tool_input 只留前 80 字，避免 Write 的整篇內容塞爆追蹤檔。"""
    text = json.dumps(tool_input or {}, ensure_ascii=False)
    return text[:INPUT_PREVIEW_LEN]


def read_jsonl(path):
    """逐行讀 jsonl；壞行略過，讓單一損毀行不會拖垮整個閘門。"""
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as jsonl_file:
        for line in jsonl_file:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def append_jsonl(path, record):
    """附加一行 JSON 到 jsonl 檔。"""
    with open(path, "a", encoding="utf-8") as jsonl_file:
        jsonl_file.write(json.dumps(record, ensure_ascii=False) + "\n")


def count_allowed(session_id, actor, tool):
    """預算只算真的放行過的次數；被擋下的不扣額度。"""
    return sum(
        1
        for rec in read_jsonl(TRACE_PATH)
        if rec.get("session_id") == session_id
        and rec.get("actor") == actor
        and rec.get("tool") == tool
        and rec.get("decision") == "allow"
    )


def load_approvals():
    """讀 approvals.json；不存在就當作沒有任何核准。"""
    if not APPROVALS_PATH.exists():
        return {"approved": []}
    with open(APPROVALS_PATH, encoding="utf-8") as approvals_file:
        approvals = json.load(approvals_file)
    if not isinstance(approvals.get("approved"), list):
        approvals["approved"] = []
    return approvals


def save_approvals(approvals):
    """寫回 approvals.json。"""
    with open(APPROVALS_PATH, "w", encoding="utf-8") as approvals_file:
        json.dump(approvals, approvals_file, ensure_ascii=False, indent=2)


def consume_approval(tool):
    """核准是一次性的：找到就移除一筆並回傳 True，避免一次點頭被重複利用。"""
    approvals = load_approvals()
    if tool not in approvals["approved"]:
        return False
    approvals["approved"].remove(tool)
    save_approvals(approvals)
    return True


def record_pending(event, actor, tool):
    """把被擋下待核准的事項記下來，讓人知道要核准什麼。"""
    append_jsonl(PENDING_PATH, {
        "ts": now_iso(),
        "session_id": event.get("session_id"),
        "actor": actor,
        "tool": tool,
        "tool_use_id": event.get("tool_use_id"),
        "input": summarize_input(event.get("tool_input")),
    })


def check_escalate(event, actor, tool):
    """escalate 工具有核准就放行（並消耗核准），沒有就擋下並記待核准。"""
    if consume_approval(tool):
        return "allow", "allow", f"已取得人工核准：{tool}（核准已消耗）"
    record_pending(event, actor, tool)
    reason = f"需要人工核准：執行 python gate.py --approve {tool} 後重跑"
    return "escalate", "deny", reason


def decide(event, policy):
    """依序跑三道檢查；回傳 (trace 決策, hook 決策, 原因)。"""
    actor = resolve_actor(event)
    tool = resolve_tool(event)
    allowed_tools = (policy["roles"].get(actor) or {}).get("tools") or []
    if tool not in allowed_tools:
        return actor, tool, ("deny", "deny", f"{actor} 的合約不允許 {tool}")
    limit = ((policy.get("budgets") or {}).get(actor) or {}).get(tool)
    if limit is not None and count_allowed(event.get("session_id"), actor, tool) >= limit:
        reason = f"預算用完：{actor} 的 {tool} 每輪最多 {limit} 次"
        return actor, tool, ("deny", "deny", reason)
    if tool in (policy.get("escalate") or []):
        return actor, tool, check_escalate(event, actor, tool)
    return actor, tool, ("allow", "allow", "通過合約與預算檢查")


def emit(permission, reason):
    """印出 Claude Code PreToolUse hook 認得的決策 JSON。"""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": permission,
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))


def write_trace(event, actor, tool, decision, reason):
    """每次決策都留一行紀錄，事後才能還原誰在何時做了什麼。"""
    append_jsonl(TRACE_PATH, {
        "ts": now_iso(),
        "session_id": event.get("session_id"),
        "actor": actor,
        "tool": tool,
        "decision": decision,
        "reason": reason,
        "input": summarize_input(event.get("tool_input")),
    })


def fail_closed(error):
    """閘門自己壞掉時寧可擋下，否則壞掉就等於全部放行。"""
    reason = f"gate 內部錯誤，預設拒絕：{type(error).__name__}: {error}"
    print(reason, file=sys.stderr)
    emit("deny", reason)
    try:
        write_trace({}, "unknown", "unknown", "deny", reason)
    except OSError:
        pass


def run_hook():
    """hook 模式：讀 stdin → 決策 → 寫追蹤 → 輸出；任何錯誤都 fail closed。"""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise TypeError("hook 輸入不是 JSON 物件")
        policy = load_policy()
        actor, tool, (decision, permission, reason) = decide(event, policy)
        write_trace(event, actor, tool, decision, reason)
        emit(permission, reason)
    except Exception as error:  # noqa: BLE001 — 閘門必須攔下所有錯誤才能 fail closed
        fail_closed(error)
    return 0


def cmd_approve(tool):
    """加入一筆一次性核准。"""
    approvals = load_approvals()
    approvals["approved"].append(tool)
    save_approvals(approvals)
    print(f"已核准一次：{tool}（目前待用核准 {approvals['approved'].count(tool)} 筆）")


def print_stats(records):
    """依角色列出 allow／deny／escalate 次數。"""
    stats = Counter((rec.get("actor"), rec.get("decision")) for rec in records)
    actors = sorted({rec.get("actor") or "?" for rec in records})
    print("\n統計：")
    for actor in actors:
        allow = stats[(actor, "allow")]
        deny = stats[(actor, "deny")]
        escalate = stats[(actor, "escalate")]
        print(f"  {actor:<11} allow={allow}  deny={deny}  escalate={escalate}")


def cmd_report(session_id):
    """把 trace.jsonl 印成表格，可只看某個 session。"""
    records = read_jsonl(TRACE_PATH)
    if session_id:
        records = [rec for rec in records if rec.get("session_id") == session_id]
    if not records:
        print("沒有追蹤紀錄。")
        return
    # 標頭含中文（全形佔兩格），所以資料欄寬刻意比標頭多 2，終端機上才對得齊
    print(f"{'時間':<19} | {'角色':<10} | {'工具':<11} | {'決策':<8} | 原因")
    print("-" * 90)
    for rec in records:
        print(f"{rec.get('ts', ''):<21} | {rec.get('actor', ''):<12} | "
              f"{rec.get('tool', ''):<13} | {rec.get('decision', ''):<10} | {rec.get('reason', '')}")
    print_stats(records)


def cmd_reset():
    """清掉所有執行期產物，回到乾淨狀態重新示範。"""
    for target in RESET_TARGETS:
        if target.is_dir():
            shutil.rmtree(target)
            print(f"已刪除目錄：{target.name}/")
        elif target.exists():
            target.unlink()
            print(f"已刪除：{target.name}")
    print("重置完成。")


def main():
    """解析參數；沒有參數就當 hook 執行。"""
    setup_utf8_io()
    parser = argparse.ArgumentParser(description="階段 3 控制閘門")
    parser.add_argument("--approve", metavar="TOOL", help="核准一次需要人工核准的動作")
    parser.add_argument("--report", action="store_true", help="印出追蹤紀錄表格")
    parser.add_argument("--session", metavar="ID", help="--report 時只看這個 session")
    parser.add_argument("--reset", action="store_true", help="刪除所有執行期產物")
    args = parser.parse_args()
    try:
        if args.approve:
            cmd_approve(args.approve)
        elif args.report:
            cmd_report(args.session)
        elif args.reset:
            cmd_reset()
        else:
            return run_hook()
    except (OSError, json.JSONDecodeError) as error:
        print(f"錯誤：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
