"""一輪＝一個工作單位：依 factory.yaml 的站序執行，每站做完立刻存檔，可中斷接續。

也包含其他子指令：init／status／dry-run／reset-calls／reset／approve。
結束碼：0＝單位完成或停在等待核准；1＝失敗或錯誤；130＝使用者中斷。
"""

import os
import shlex
import sys
import time

from lhf import claude_call, stations
from lhf.config import FINAL_STATUSES, posix, render, render_path
from lhf.errors import CallBudgetExceeded, RoundError
from lhf.state import (
    IN_PROGRESS,
    PENDING_APPROVAL,
    approved_path,
    alerts_path,
    append_jsonl,
    blocked_reason,
    load_state,
    log_event,
    new_state,
    new_unit,
    now_str,
    pending_approvals_path,
    read_jsonl,
    run_path,
    runnable_units,
    write_heartbeat,
    write_jsonl,
)

EXIT_INTERRUPTED = 130
USD_TO_TWD = 32.0  # 只為了讓摘要好讀的概略匯率；走 Max 訂閱不實扣，這是「等值成本」


# ---------- 執行一輪 ----------


def maybe_fake_interrupt(station_id):
    """測試鉤子：Windows 很難從腳本對子程序送真正的 Ctrl+C，用這個模擬。"""
    if os.environ.get("LH_FAKE_INTERRUPT_AFTER") == station_id:
        print(f"    （測試鉤子）站 {station_id} 完成後模擬 Ctrl+C", flush=True)
        raise KeyboardInterrupt


def run_stations(rt, summary):
    """依 rec["station"] 一站一站走；退回會把 station 改回 producer，迴圈自然回頭。"""
    while True:
        rec = stations.unit_rec(rt)
        if rec["status"] in FINAL_STATUSES or rec["status"] == PENDING_APPROVAL:
            return
        station = rt["cfg"].station(rec["station"])
        print(f"[站 {stations.label_of(station)}]", flush=True)
        start = time.monotonic()
        result = stations.STATION_FUNCS[station["kind"]](rt, station)
        summary.append((stations.label_of(station), result, time.monotonic() - start))
        maybe_fake_interrupt(station["id"])


def pick_unit(cfg, state, unit_arg):
    if unit_arg:
        reason = blocked_reason(cfg, state, unit_arg)
        if reason:
            print(reason)
            return None
        return unit_arg
    units = runnable_units(cfg, state)
    return units[0] if units else None


def begin_round(cfg, state, unit):
    """新單位才開新一輪；接續中的單位沿用原本的輪次與 claude -p 次數。"""
    rec = state["units"][unit]
    if rec["round"] is None:
        state["round"] += 1
        rec["round"], rec["started"], rec["status"] = state["round"], now_str(), IN_PROGRESS
        log_event(cfg, state, unit, f"第 {rec['round']} 輪開始")
    else:
        log_event(cfg, state, unit, f"接續第 {rec['round']} 輪（目前站：{rec['station']}，已呼叫 {rec['calls']} 次）")
    return {"cfg": cfg, "state": state, "unit": unit, "round": rec["round"]}


def heartbeat(rt, outcome, elapsed):
    rec = stations.unit_rec(rt)
    write_heartbeat(
        rt["cfg"],
        f"{now_str()}｜第 {rec['round']} 輪｜{rt['unit']}｜{outcome}｜claude -p {rec['calls']} 次｜{elapsed:.0f} 秒",
    )


def print_summary(rt, summary, elapsed):
    rec, cost = stations.unit_rec(rt), stations.unit_rec(rt)["cost_usd"]
    retries = "　".join(f"{name} {count} 次" for name, count in rec["retries"].items()) or "無退回"
    print("\n" + "=" * 56)
    print(f"本輪摘要：第 {rec['round']} 輪｜{rt['unit']}｜狀態 {rec['status']}（站 {rec['station']}）")
    for label, result, seconds in summary:
        print(f"  站 {label:<14} {result}（{seconds:.1f} 秒）")
    print(f"  claude -p 次數：{rec['calls']}／{rt['cfg'].limits['calls_per_unit']}　退回：{retries}")
    print(f"  等值成本：US$ {cost:.4f}（約 NT$ {cost * USD_TO_TWD:.1f}，走 Max 訂閱不實扣）")
    print(f"  本次耗時：{elapsed:.1f} 秒")
    print("=" * 56, flush=True)


def outcome_of(status):
    if status == "done":
        return "成功", 0
    if status == PENDING_APPROVAL:
        return "等待核准", 0
    return "失敗", 1


def cmd_run(cfg, unit_arg=None):
    state = load_state(cfg)
    if state is None:
        raise RoundError("沒有 state.json，先執行 init")
    unit = pick_unit(cfg, state, unit_arg)
    if unit is None:
        if not unit_arg:
            print("queue 裡沒有可執行的工作單位")
        return 0
    rt, start, summary = begin_round(cfg, state, unit), time.monotonic(), []
    try:
        run_stations(rt, summary)
    except KeyboardInterrupt:
        return handle_interrupt(rt, summary, time.monotonic() - start)
    except CallBudgetExceeded as exc:
        stations.finish(rt, "failed", f"停止本單位：{exc}", alert=True)
    except RoundError as exc:
        return handle_error(rt, summary, exc, time.monotonic() - start)
    elapsed = time.monotonic() - start
    outcome, code = outcome_of(stations.unit_rec(rt)["status"])
    heartbeat(rt, outcome, elapsed)
    print_summary(rt, summary, elapsed)
    return code


def handle_error(rt, summary, exc, elapsed):
    station = stations.unit_rec(rt)["station"]
    log_event(rt["cfg"], rt["state"], rt["unit"], f"錯誤（停在站 {station}，下次重試）：{exc}")
    heartbeat(rt, "失敗", elapsed)
    print_summary(rt, summary, elapsed)
    print(f"錯誤：{exc}", file=sys.stderr, flush=True)
    return 1


def handle_interrupt(rt, summary, elapsed):
    """進度在每站結束時就存好了，這裡只補一筆紀錄與 heartbeat。"""
    station = stations.unit_rec(rt)["station"]
    try:
        log_event(rt["cfg"], rt["state"], rt["unit"], f"使用者中斷（停在站 {station}）")
        heartbeat(rt, "中斷", elapsed)
    except OSError as exc:
        print(f"（中斷時寫紀錄失敗：{exc}）", file=sys.stderr)
    print_summary(rt, summary, elapsed)
    print(f"\n⛔ 中斷！進度已存在 state.json（{rt['unit']} 停在站 {station}），再執行一次會從這一站接著做")
    return EXIT_INTERRUPTED


# ---------- 其他指令 ----------


def cmd_init(cfg, unit_ids, force):
    """沒給 id 就用 factory.yaml 的全部單位；id 必須在 units.ids 裡（依賴關係才有意義）。"""
    unit_ids = list(dict.fromkeys(unit_ids or cfg.unit_ids))
    unknown = [unit for unit in unit_ids if unit not in cfg.unit_ids]
    if unknown:
        raise RoundError(f"這些單位不在 factory.yaml 的 units.ids：{unknown}")
    state = load_state(cfg) or new_state()
    state["queue"] = unit_ids
    for unit in unit_ids:
        if force or unit not in state["units"]:
            state["units"][unit] = new_unit(cfg)
    log_event(cfg, state, "-", f"建立 queue：{' '.join(unit_ids)}{'（--force 重設）' if force else ''}")
    print(f"queue：{' '.join(unit_ids)}")
    return 0


def require_unit(cfg, unit):
    state = load_state(cfg)
    if state is None:
        raise RoundError("沒有 state.json，先執行 init")
    if unit not in state["units"]:
        raise RoundError(f"{unit} 不在 state.json")
    return state


def cmd_status(cfg):
    state = load_state(cfg)
    if state is None:
        print("沒有 state.json（尚未 init）")
        return 0
    print(f"專案 {cfg.name}｜目前輪次：{state['round']}｜queue：{' '.join(state['queue']) or '（空）'}")
    for unit in state["queue"]:
        rec = state["units"].get(unit) or new_unit(cfg)
        retries = ",".join(f"{k}={v}" for k, v in rec["retries"].items()) or "-"
        note = blocked_reason(cfg, state, unit) if rec["status"] not in FINAL_STATUSES else ""
        print(
            f"  {unit:<12} {rec['status']:<16} 站 {rec['station']:<10} 輪 {rec['round'] or '-':<3} "
            f"呼叫 {rec['calls']:>2}  退回 {retries:<24} US$ {rec['cost_usd']:.4f}  {note or ''}"
        )
    print_status_tail(cfg, state)
    return 0


def print_status_tail(cfg, state):
    print("最近 history：")
    for item in state["history"][-8:]:
        print(f"  {item['ts']} [第 {item['round']} 輪 {item['unit']}] {item['event']}")
    pending, alerts = read_jsonl(pending_approvals_path(cfg)), read_jsonl(alerts_path(cfg))
    if pending:
        print(f"等待核准：{len(pending)} 筆（{', '.join(sorted({p.get('unit', '?') for p in pending}))}）")
    for alert in alerts[-5:]:
        print(f"⚠ ALERT {alert['ts']} {alert['unit']} {alert['status']}：{alert['reason']}")
    heartbeat_path = run_path(cfg, "heartbeat.txt")
    if heartbeat_path.exists():
        print("heartbeat：" + heartbeat_path.read_text(encoding="utf-8").strip())


def cmd_reset_calls(cfg, unit):
    """hook 沒掛上等環境問題白白用掉次數時，修好後歸零重跑，並留 history 方便追查。"""
    state = require_unit(cfg, unit)
    rec = state["units"][unit]
    old_calls, rec["calls"] = rec.get("calls", 0), 0
    log_event(cfg, state, unit, f"手動歸零 claude -p 次數（{old_calls} → 0，reset-calls）")
    print(f"{unit} 的 claude -p 次數已從 {old_calls} 歸零（狀態維持 {rec['status']}）")
    return 0


def cmd_reset(cfg, unit):
    """人看過 ALERT 後，把單位整個重設回起點（failed／rejected 不會自動重做）。"""
    state = require_unit(cfg, unit)
    old_status = state["units"][unit]["status"]
    state["units"][unit] = new_unit(cfg)
    log_event(cfg, state, unit, f"手動重設（原狀態 {old_status} → pending，reset）")
    print(f"{unit} 已重設為 pending")
    return 0


def cmd_approve(cfg, unit):
    """把該單位的 pending 移到 approved；站型核准記在 rec，指令核准給 gate 比對。"""
    state = require_unit(cfg, unit)
    rec = state["units"][unit]
    if rec["status"] != PENDING_APPROVAL:
        raise RoundError(f"{unit} 目前是 {rec['status']}，沒有在等待核准")
    pending = read_jsonl(pending_approvals_path(cfg))
    mine = [item for item in pending if item.get("unit") == unit]
    for item in mine:
        append_jsonl(approved_path(cfg), {**item, "approved_at": now_str()})
    write_jsonl(pending_approvals_path(cfg), [item for item in pending if item.get("unit") != unit])
    station_id = (rec.get("approval") or {}).get("station") or rec["station"]
    if cfg.station(station_id)["kind"] == "approve" and station_id not in rec["approved"]:
        rec["approved"].append(station_id)
    rec["status"], rec["approval"] = IN_PROGRESS, None
    log_event(cfg, state, unit, f"人工核准 {len(mine)} 筆（站 {station_id}），下次執行重跑該站")
    print(f"{unit} 已核准，下次 run 會從站 {station_id} 繼續")
    return 0


# ---------- dry-run ----------


def dry_run_station(rt, station):
    cfg, mapping = rt["cfg"], stations.mapping_of(rt)
    kind = station["kind"]
    if kind in ("agent", "parallel"):
        specs = [station] if kind == "agent" else station["jobs"]
        for spec in specs:
            prompt = stations.prompt_text(rt, spec["prompt"])
            cmd = claude_call.describe_cmd(cfg, rt, spec["role"])
            print(f"  [{spec['role']}] LH_ROLE={spec['role']} LH_UNIT={rt['unit']} {shlex.join(cmd)} < prompt（stdin）")
            print(f"    prompt 摘要（{len(prompt)} 字）：{prompt[:160].replace(chr(10), ' ')}…")
    elif kind == "command":
        for title, argv, _ in stations.expand_cmds(station, mapping):
            print(f"  [{title}] cd {render_path(station.get('cwd') or '{WORK}', mapping)} && {shlex.join(argv)}")
    elif kind == "commit":
        files = " ".join(stations.render_list(station["paths"], mapping))
        print(f"  [commit] git -C {posix(cfg.work_dir())} add -- {files} && git commit -- …（不 push）")
    else:
        print(f"  [approve] 停下等人工核准：{render(station.get('message') or '', mapping)}")


def cmd_dry_run(cfg, unit_arg=None):
    """只印不跑：不寫 state、不呼叫模型、不執行指令或 git。"""
    state = load_state(cfg) or new_state()
    unit = unit_arg or pick_unit(cfg, state, None) or (cfg.unit_ids[0] if not state["queue"] else None)
    if unit is None:
        raise RoundError("沒有可預覽的單位：先 init 或加 --unit")
    rec = state["units"].get(unit) or new_unit(cfg)
    rt = {"cfg": cfg, "state": {"units": {unit: rec}}, "unit": unit, "round": rec["round"] or state["round"] + 1}
    print(
        f"（dry-run）{cfg.name}｜{unit} 第 {rt['round']} 輪｜工作目錄 {posix(cfg.work_dir())}｜目前站：{rec['station']}"
    )
    for station in cfg.stations:
        print(f"[站 {stations.label_of(station)}]（{station['kind']}／{station['chain']}）")
        dry_run_station(rt, station)
    return 0
