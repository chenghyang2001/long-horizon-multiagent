"""state.json／history／heartbeat／trace／approvals／alerts 的讀寫，以及挑下一個工作單位。

所有檔案都在 <project>/run/ 底下。state.json 每次都先寫 .tmp 再 os.replace（原子操作），
中斷時不會留下寫一半的檔；這就是「每站做完立刻存檔、可中斷接續」的基礎。
"""

import json
import os
from datetime import datetime

from lhf.config import FINAL_STATUSES
from lhf.errors import RoundError

PENDING_APPROVAL = "pending_approval"
IN_PROGRESS = "in_progress"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
    if not isinstance(state, dict) or not isinstance(state.get("units"), dict):
        raise RoundError(f"state.json 格式不對（{path}）：缺少 units")
    return state


def save_state(cfg, state):
    ensure_run_dir(cfg)
    path = run_path(cfg, "state.json")
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def log_event(cfg, state, unit, event):
    """記一筆 history 並立刻存檔——這就是 checkpoint。"""
    round_no = state["units"].get(unit, {}).get("round") or state["round"]
    state["history"].append({"ts": now_str(), "round": round_no, "unit": unit, "event": event})
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
        return f"{unit} 等待人工核准；確認後執行 approve {unit}"
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
    for line in text.splitlines():
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
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    os.replace(tmp_path, path)


def trace_path(cfg):
    return run_path(cfg, "trace.jsonl")


def pending_approvals_path(cfg):
    return run_path(cfg, "approvals") / "pending.jsonl"


def approved_path(cfg):
    return run_path(cfg, "approvals") / "approved.jsonl"


def alerts_path(cfg):
    return run_path(cfg, "alerts.jsonl")


def write_engine_trace(cfg, rt, kind, decision, reason):
    """引擎自己的紀錄（verdict／退回）也寫進 trace，role 固定為 engine，不影響 gate 預算計算。"""
    append_jsonl(
        trace_path(cfg),
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
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
    """超過退回上限的單位進 ALERT 清單；下一輪不會自動重做，要人看過後 reset。"""
    append_jsonl(alerts_path(cfg), {"ts": now_str(), "unit": unit, "status": status, "reason": reason})
