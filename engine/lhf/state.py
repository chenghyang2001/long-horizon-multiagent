"""state.json／history／heartbeat／trace／approvals／alerts 的讀寫，以及挑下一個工作單位。

所有檔案都在 <project>/run/ 底下。state.json 每次都先寫 .<pid>.tmp 再原子取代，
中斷時不會留下寫一半的檔；這就是「每站做完立刻存檔、可中斷接續」的基礎。
外部指令佇列 commands.jsonl：引擎執行中時 approve／reset／reset-calls 寫進佇列，引擎每站開始時套用，
避免兩個程序各自讀改寫 state.json 而互相覆蓋。
"""

import json
import os
import secrets
import time

from lhf.common import atomic_replace, now_iso, now_str, tmp_name
from lhf.config import FINAL_STATUSES
from lhf.errors import RoundError

PENDING_APPROVAL = "pending_approval"
IN_PROGRESS = "in_progress"


def run_path(cfg, name):
    return cfg.run_dir / name


def ensure_run_dir(cfg):
    cfg.run_dir.mkdir(parents=True, exist_ok=True)


# ---------- state.json ----------


def new_state():
    return {"round": 0, "queue": [], "units": {}, "history": []}


def new_unit(cfg):
    """retries 依計數器名稱（rewrites／review_retries）累計；once_done 記錄只需做一次的角色。"""
    return {
        "status": "pending",
        "station": cfg.stations[0]["id"],
        "retries": {},
        "verdicts": [],
        "calls": 0,
        "started": None,
        "finished": None,
        "round": None,
        "once_done": [],
        "feedback": None,
        "cost_usd": 0.0,
        "sessions": [],
        "approved": [],
        "approval": None,
        "engine_errors": 0,
        "commits": [],
    }


def load_state(cfg):
    """壞檔直接報錯，不默默重建，以免蓋掉多輪累積的進度。"""
    path = run_path(cfg, "state.json")
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RoundError(f"state.json 讀取失敗（{path}）：{exc}") from exc
    problem = state_problem(cfg, state)
    if problem:
        raise RoundError(f"state.json 格式不對（{path}）：{problem}；請修正或備份後刪除再 init，引擎不會覆寫它")
    return state


# 每個單位必須有的欄位與型別；缺任何一個就拒絕使用（不猜預設值，避免默默蓋掉進度）
UNIT_FIELDS = {
    "status": str,
    "station": str,
    "retries": dict,
    "verdicts": list,
    "calls": int,
    "once_done": list,
    "sessions": list,
    "approved": list,
    "cost_usd": (int, float),
}
VALID_STATUSES = {"pending", IN_PROGRESS, PENDING_APPROVAL} | set(FINAL_STATUSES)


def state_problem(cfg, state):
    """回傳第一個發現的問題描述；沒問題回 None。"""
    if not isinstance(state, dict):
        return "最外層不是物件"
    for key, kind in (("round", int), ("queue", list), ("units", dict), ("history", list)):
        if not isinstance(state.get(key), kind) or isinstance(state.get(key), bool):
            return f"缺少或型別錯誤的欄位 {key}"
    missing_units = [unit for unit in state["queue"] if unit not in state["units"]]
    if missing_units:
        return f"queue 裡的 {missing_units} 不在 units"
    for unit, rec in state["units"].items():
        problem = unit_problem(cfg, rec)
        if problem:
            return f"單位 {unit}：{problem}"
    return None


def unit_problem(cfg, rec):
    if not isinstance(rec, dict):
        return "不是物件"
    for key, kind in UNIT_FIELDS.items():
        if key not in rec:
            return f"缺少欄位 {key}"
        if not isinstance(rec[key], kind) or isinstance(rec[key], bool):
            return f"欄位 {key} 型別錯誤"
    if rec["status"] not in VALID_STATUSES:
        return f"未知狀態 {rec['status']!r}"
    if rec["station"] not in {station["id"] for station in cfg.stations}:
        return f"站 {rec['station']!r} 不在 factory.yaml（設定改過站名？）"
    return None


def save_state(cfg, state):
    ensure_run_dir(cfg)
    path = run_path(cfg, "state.json")
    tmp_path = tmp_name(path)
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    atomic_replace(tmp_path, path)


def log_event(cfg, state, unit, event):
    """記一筆 history 並立刻存檔——這就是 checkpoint。history 只留最近 history_max 筆，避免無限長大。"""
    round_no = state["units"].get(unit, {}).get("round") or state["round"]
    state["history"].append({"ts": now_str(), "round": round_no, "unit": unit, "event": event})
    del state["history"][: -cfg.limits["history_max"]]
    save_state(cfg, state)
    print(f"    · {event}", flush=True)


def write_heartbeat(cfg, line):
    ensure_run_dir(cfg)
    run_path(cfg, "heartbeat.txt").write_text(line + "\n", encoding="utf-8")


# ---------- 挑單位 ----------


def deps_done(cfg, state, unit):
    units = state["units"]
    return all(units.get(dep, {}).get("status") == "done" for dep in cfg.depends_on.get(unit) or [])


def is_runnable(cfg, state, unit):
    rec = state["units"].get(unit)
    if rec is None or rec["status"] in FINAL_STATUSES or rec["status"] == PENDING_APPROVAL:
        return False
    return deps_done(cfg, state, unit)


def runnable_units(cfg, state):
    """依 queue 順序列出現在可以跑的單位；依賴未完成、等待核准、已結束的都排除。"""
    return [unit for unit in state.get("queue", []) if is_runnable(cfg, state, unit)]


def blocked_reason(cfg, state, unit):
    """說明單位為什麼不能跑；可以跑回 None。"""
    rec = state["units"].get(unit)
    if rec is None:
        return f"{unit} 不在 state.json，先 init {unit}"
    if rec["status"] in FINAL_STATUSES:
        return f"{unit} 已是 {rec['status']}；要重跑請 reset {unit}"
    if rec["status"] == PENDING_APPROVAL:
        return f"{unit} 等待核准，執行 approve {unit}（先確認 run/approvals/pending.jsonl 的內容）"
    waiting = [dep for dep in cfg.depends_on.get(unit) or [] if state["units"].get(dep, {}).get("status") != "done"]
    if waiting:
        return f"{unit} 依賴的 {'、'.join(waiting)} 尚未完成"
    return None


# ---------- jsonl（trace／approvals／alerts） ----------


def append_jsonl(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path):
    """壞行（另一個程序寫到一半）直接略過。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    return parse_jsonl(text)


def parse_jsonl(text):
    records = []
    for line in text.split("\n"):  # 不用 splitlines：它會在 U+2028 等字元斷行，讓含這些字元的紀錄整筆消失
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def file_offset(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def read_jsonl_since(path, offset):
    """用位元組位置讀新增部分；檔案比 offset 小代表被清過，從頭讀。"""
    try:
        with open(path, "rb") as handle:
            if handle.seek(0, os.SEEK_END) < offset:
                offset = 0
            handle.seek(offset)
            return parse_jsonl(handle.read().decode("utf-8", "replace"))
    except FileNotFoundError:
        return []


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_name(path)
    tmp_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    atomic_replace(tmp_path, path)


def trace_path(cfg):
    return run_path(cfg, "trace.jsonl")


def pending_approvals_path(cfg):
    return run_path(cfg, "approvals") / "pending.jsonl"


def approved_path(cfg):
    return run_path(cfg, "approvals") / "approved.jsonl"


def approvals_used_path(cfg):
    return run_path(cfg, "approvals") / "used.jsonl"


def alerts_path(cfg):
    return run_path(cfg, "alerts.jsonl")


def commands_path(cfg):
    return run_path(cfg, "commands.jsonl")


def queue_command(cfg, command, unit):
    """引擎執行中：把 approve／reset／reset-calls 排進佇列，由引擎下一站開始時套用。每筆帶唯一 id。
    ancestry_checked＝排入前已確認提交者不是引擎的子孫（呼叫端 round.guard_human 負責檢查）。"""
    record = {
        "id": secrets.token_hex(8),
        "ts": now_str(),
        "command": command,
        "unit": unit,
        "pid": os.getpid(),
        "ancestry_checked": True,
    }
    append_jsonl(commands_path(cfg), record)
    return record


def command_known(cfg, record_id):
    """排入後確認指令還在系統裡。依指令在引擎裡的流向讀：佇列 → 認領檔 → state 的 applied_commands。
    引擎是「改名認領 → 套用並存 state → 刪認領檔」，照同樣順序讀，同一筆指令至少會在其中一處被看到；
    唯一的例外是被延後（正在跑的單位不能 reset）而放回佇列的指令，所以呼叫端重排時一律沿用同一個 id，
    重複的 id 引擎只會套用一次，不會造成重複套用。"""
    if any(r.get("id") == record_id for r in read_jsonl(commands_path(cfg))):
        return True
    for path in sorted(cfg.run_dir.glob("commands.processing-*.jsonl")):
        if any(r.get("id") == record_id for r in read_jsonl(path)):
            return True
    try:
        state = json.loads(run_path(cfg, "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    applied = state.get("applied_commands") if isinstance(state, dict) else None
    return isinstance(applied, list) and record_id in applied


def command_id(record):
    """舊格式（沒有 id）用內容組一個穩定的 id，才能判斷是否套用過。"""
    return record.get("id") or "|".join(str(record.get(k)) for k in ("ts", "command", "unit", "pid"))


def claim_commands(cfg):
    """把目前的佇列原子地改名認領，連同上次沒處理完（當機留下）的認領檔一起回傳。
    只有持有執行鎖的引擎會呼叫，所以不會有兩個程序同時處理同一個認領檔。
    認領檔要等套用結果存進 state.json 之後才刪（見 round.apply_queued），中途當機也不會遺失指令。"""
    path = commands_path(cfg)
    if path.exists():
        claimed = path.with_name(f"commands.processing-{os.getpid()}-{time.time_ns()}.jsonl")
        try:
            atomic_replace(path, claimed)
        except (FileNotFoundError, PermissionError):
            pass  # 別人正在寫入：這一站先不認領，下一站再拿
    return sorted(cfg.run_dir.glob("commands.processing-*.jsonl"))


def requeue_commands(cfg, records):
    for record in records:
        append_jsonl(commands_path(cfg), record)


def write_engine_trace(cfg, rt, kind, decision, reason):
    """引擎自己的紀錄（verdict／退回）也寫進 trace，role 固定為 engine，不影響 gate 預算計算。"""
    append_jsonl(
        trace_path(cfg),
        {
            "ts": now_iso(),
            "round": str(rt["round"]),
            "unit": rt["unit"],
            "session": "",
            "role": "engine",
            "tool": "",
            "kind": kind,
            "decision": decision,
            "reason": reason,
            "input": "",
        },
    )


def add_alert(cfg, unit, status, reason):
    """超過退回上限的單位進 ALERT 清單；下一輪不會自動重做，要人看過後 reset。
    factory.yaml 設了 notify.telegram.on 含 alert 時同步發 Telegram（失敗只警告，不影響引擎）。"""
    from lhf import notify

    reason = notify.redact(reason)  # alerts.jsonl 進版控：通知金鑰一律遮蔽
    append_jsonl(alerts_path(cfg), {"ts": now_str(), "unit": unit, "status": status, "reason": reason})

    notify.send(cfg, "alert", title=f"ALERT（{status}）", unit=unit, reason=reason)
