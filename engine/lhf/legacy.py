"""舊版進度轉移：把第一代主控程式的 state.json 轉成本引擎的格式。

舊格式：{"round", "queue", "chapters": {id: {status, rewrites, review_retries, verdicts, calls, cost_usd, ...}},
        "history": [{"ts", "round", "chapter", "event"}]}
新格式：{"round", "queue", "units": {...}, "history": [{"ts", "round", "unit", "event"}]}

轉移規則（不猜中間狀態，寧可重做也不要從錯的站接續）：
- done／failed／rejected：狀態保留，round／calls／cost_usd／verdicts／sessions／started／finished 照搬，
  rewrites、review_retries 搬進 retries；站設為最後一站（done）或第一站（failed／rejected，reset 後重來）
- 其他（做到一半）：重設為 pending，history 記一筆原狀態，下次從第一站重做
- factory.yaml 沒有的單位：略過並列出
注意：本模組是唯一需要認得舊欄位名的地方。
"""

import json
from pathlib import Path

from lhf import lock
from lhf.common import now_str
from lhf.config import FINAL_STATUSES
from lhf.errors import RoundError
from lhf.state import load_state, new_state, new_unit, run_path, save_state

LEGACY_UNITS_KEY = "chapters"
LEGACY_UNIT_FIELD = "chapter"
COPY_FIELDS = ("round", "calls", "cost_usd", "verdicts", "sessions", "started", "finished")


def read_legacy(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RoundError(f"讀不到舊版 state（{path}）：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get(LEGACY_UNITS_KEY), dict):
        raise RoundError(f"{path} 不是舊版格式（缺少 {LEGACY_UNITS_KEY}）")
    return data


def convert_unit(cfg, old):
    rec = new_unit(cfg)
    status = old.get("status")
    if status not in FINAL_STATUSES:
        return rec, f"原狀態 {status}，重設為 pending"
    for key in COPY_FIELDS:
        if old.get(key) is not None:
            rec[key] = old[key]
    rec["status"] = status
    rec["retries"] = {name: int(old.get(name) or 0) for name in ("rewrites", "review_retries") if old.get(name)}
    rec["station"] = cfg.stations[-1]["id"] if status == "done" else cfg.stations[0]["id"]
    rec["calls"], rec["cost_usd"] = int(rec["calls"] or 0), float(rec["cost_usd"] or 0.0)
    return rec, f"保留 {status}"


def convert_history(old_history):
    history = []
    for item in old_history or []:
        if isinstance(item, dict):
            history.append(
                {
                    "ts": item.get("ts", ""),
                    "round": item.get("round"),
                    "unit": item.get("unit") or item.get(LEGACY_UNIT_FIELD) or "-",
                    "event": str(item.get("event", "")),
                }
            )
    return history


def build_state(cfg, legacy):
    state, notes, skipped = new_state(), [], []
    state["round"] = int(legacy.get("round") or 0)
    for unit, old in legacy[LEGACY_UNITS_KEY].items():
        if unit not in cfg.unit_ids or not isinstance(old, dict):
            skipped.append(unit)
            continue
        state["units"][unit], note = convert_unit(cfg, old)
        notes.append(f"{unit}：{note}")
    order = [u for u in legacy.get("queue") or [] if u in state["units"]]
    state["queue"] = order + [u for u in state["units"] if u not in order]
    state["history"] = convert_history(legacy.get("history"))
    return state, notes, skipped


def cmd_migrate(cfg, source, force):
    with lock.holding(cfg, "migrate"):
        if load_state(cfg) is not None and not force:
            raise RoundError(f"{run_path(cfg, 'state.json')} 已存在；確定要覆蓋請加 --force")
        state, notes, skipped = build_state(cfg, read_legacy(source))
        state["history"].append(
            {
                "ts": now_str(),
                "round": state["round"],
                "unit": "-",
                "event": f"由舊版 state 轉移（{Path(source).as_posix()}），{len(notes)} 個單位",
            }
        )
        save_state(cfg, state)
    for line in notes:
        print(f"  {line}")
    if skipped:
        print(f"  略過（factory.yaml 沒有）：{'、'.join(skipped)}")
    print(f"已寫入 {run_path(cfg, 'state.json')}（輪次 {state['round']}）")
    return 0
