"""一輪＝一個工作單位：依 factory.yaml 的站序執行，每站做完立刻存檔，可中斷接續。

也包含其他子指令：init／status／dry-run／reset-calls／reset／approve。
run 結束碼：0＝單位完成或停在等待核准；1＝單位被判 failed／rejected；3＝引擎錯誤（停在原站，下次重試）；
            4＝已有另一個 run／hours 在跑；130＝使用者中斷。
同一單位連續引擎錯誤達 limits.max_engine_errors（預設 3）次 → 標 failed＋ALERT，換下一個單位。

併行安全：run 全程持有執行鎖；approve／reset／reset-calls 遇到鎖被別人持有時改寫進 commands.jsonl，
由正在跑的引擎在每站開始時套用，不會兩邊各自讀改寫 state.json 而互相覆蓋。
"""

import os
import shlex
import sys
import time

from lhf import ancestry, claude_call, integrity, lock, proc, stations, stop
from lhf.common import (
    EXIT_CONTROL_CHANGED,
    EXIT_ENGINE_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_UNIT_FAILED,
    IN_ENGINE_ENV,
    USD_TO_TWD,
    now_str,
)
from lhf.config import FINAL_STATUSES, posix, render, render_path
from lhf.errors import CallBudgetExceeded, EngineDescendant, RoundError, StopRequested
from lhf.snapshot import write_snapshot
from lhf.state import (
    IN_PROGRESS,
    PENDING_APPROVAL,
    add_alert,
    alerts_path,
    append_jsonl,
    approved_path,
    blocked_reason,
    claim_commands,
    command_id,
    command_known,
    commands_path,
    load_state,
    log_event,
    new_state,
    new_unit,
    pending_approvals_path,
    queue_command,
    read_jsonl,
    requeue_commands,
    run_path,
    runnable_units,
    save_state,
    write_heartbeat,
    write_jsonl,
)

# ---------- 會改 state 的指令（直接執行與佇列套用共用同一份邏輯） ----------


def apply_reset_calls(cfg, state, unit):
    """hook 沒掛上等環境問題白白用掉次數時，修好後歸零重跑，並留 history 方便追查。"""
    rec = state["units"][unit]
    old_calls, rec["calls"] = rec.get("calls", 0), 0
    log_event(cfg, state, unit, f"手動歸零 claude -p 次數（{old_calls} → 0，reset-calls）")
    return f"{unit} 的 claude -p 次數已從 {old_calls} 歸零（狀態維持 {rec['status']}）"


def apply_reset(cfg, state, unit):
    """人看過 ALERT 後，把單位整個重設回起點（failed／rejected 不會自動重做）。"""
    old_status = state["units"][unit]["status"]
    state["units"][unit] = new_unit(cfg)
    log_event(cfg, state, unit, f"手動重設（原狀態 {old_status} → pending，reset）")
    return f"{unit} 已重設為 pending"


def apply_approve(cfg, state, unit):
    """把該單位的 pending 移到 approved；站型核准記在 rec，指令核准給 gate 比對（綁輪次、用一次）。"""
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
    return f"{unit} 已核准，下次 run 會從站 {station_id} 繼續"


MUTATIONS = {"approve": apply_approve, "reset": apply_reset, "reset-calls": apply_reset_calls}
APPLIED_KEEP = 500  # state 只記最近幾筆已套用的佇列指令 id


def require_unit(cfg, unit):
    state = load_state(cfg)
    if state is None:
        raise RoundError("沒有 state.json，先執行 init")
    if unit not in state["units"]:
        raise RoundError(f"{unit} 不在 state.json")
    return state


def precheck(cfg, command, unit):
    """排進佇列前先做唯讀檢查，明顯錯誤（單位不存在、沒在等核准）立刻告訴使用者。"""
    state = require_unit(cfg, unit)
    if command == "approve" and state["units"][unit]["status"] != PENDING_APPROVAL:
        raise RoundError(f"{unit} 目前是 {state['units'][unit]['status']}，沒有在等待核准")


def guard_human(cfg, command):
    """人工指令只能由人發出：三層檢查。
    1. 環境旗標（引擎子程序都帶）：快，但可被 env -u 清掉
    2. 具名引擎 Job（Windows）：start /b、Start-Process、孤兒程序都脫離不了 job
    3. 祖先鏈：持鎖的引擎程序在我的祖先裡＝我是引擎底下的程序（指令站腳本、代理的 Bash）"""
    if os.environ.get(IN_ENGINE_ENV) == "1":
        raise EngineDescendant(f"引擎底下的程序不可下人工指令（{command}）：偵測到 {IN_ENGINE_ENV}")
    owner = lock.read_lock(cfg)
    if owner and lock.lock_alive(owner) and proc.in_named_job(owner.get("job")):
        raise EngineDescendant(f"引擎底下的程序不可下人工指令（{command}）：目前程序在引擎的 Job 裡")
    if owner and lock.lock_alive(owner) and ancestry.is_descendant_of(owner.get("pid")):
        raise EngineDescendant(f"引擎底下的程序不可下人工指令（{command}）：引擎 PID {owner.get('pid')} 在祖先程序鏈上")


STATION_WAIT_SEC = 3600  # 最多等一個指令站跑完；超過就放棄（請稍後再下一次指令）


def wait_station_idle(cfg):
    """指令站執行期間佇列檔被改會被當成竄改；所以人下的指令要等指令站跑完才排入。
    只認「有效」標記（標記的程序與持鎖程序都還活著）：當掉留下的殘留標記不會讓人白等一小時。"""
    deadline = time.monotonic() + STATION_WAIT_SEC
    announced = False
    while integrity.active_marker(cfg) is not None:
        if time.monotonic() > deadline:
            raise RoundError(f"等了 {STATION_WAIT_SEC} 秒指令站還沒跑完，請稍後再下一次指令")
        if not announced:
            print("指令站執行中，等它跑完再排入佇列…", flush=True)
            announced = True
        time.sleep(1)


def queue_when_idle(cfg, command, unit, record=None):
    """在佇列互斥檔內確認「沒有指令站在跑」才寫入（引擎也在同一個互斥檔內放標記，兩邊不會交錯）。
    record 有值＝重排同一筆（同一個 id，引擎只會套用一次）。"""
    deadline = time.monotonic() + STATION_WAIT_SEC
    while True:
        wait_station_idle(cfg)
        with integrity.queue_mutex(cfg):
            if integrity.active_marker(cfg) is None:
                if record is None:
                    return queue_command(cfg, command, unit)
                append_jsonl(commands_path(cfg), record)
                return record
        if time.monotonic() > deadline:
            raise RoundError(f"等了 {STATION_WAIT_SEC} 秒指令站還沒跑完，請稍後再下一次指令")


def queue_verified(cfg, command, unit):
    """排入後再確認一次：指令若已不在系統裡（被指令站的竄改還原掉），用同一個 id 重排一次。"""
    record = queue_when_idle(cfg, command, unit)
    if not command_known(cfg, record["id"]):
        print("（排入的指令不見了，重新排入同一筆）", flush=True)
        queue_when_idle(cfg, command, unit, record=record)
    return record


def cmd_mutate(cfg, command, unit):
    """引擎在跑 → 排進 commands.jsonl；沒在跑 → 拿鎖後直接讀改寫。兩種都先確認是人下的指令。"""
    guard_human(cfg, command)
    owner = lock.held_by_other(cfg)
    if owner:
        precheck(cfg, command, unit)
        queue_verified(cfg, command, unit)
        print(f"引擎正在執行（PID {owner.get('pid')}），{command} {unit} 已排入 commands.jsonl，下一站開始時套用")
        return EXIT_OK
    with lock.holding(cfg, command):
        print(MUTATIONS[command](cfg, require_unit(cfg, unit), unit))
    return EXIT_OK


def apply_queued(rt):
    """每站開始時套用外部指令（含上次當機沒處理完的認領檔）。
    冪等：每筆的 id 先記進 state 再套用，同一個 state.json 存檔一起落地；重讀到已套用的 id 直接略過。
    正在跑的單位不能 reset（會打斷本站），先放回佇列等它不在跑的時候。"""
    cfg, state = rt["cfg"], rt["state"]
    applied = state.setdefault("applied_commands", [])
    for path in claim_commands(cfg):
        deferred = []
        for record in read_jsonl(path):
            if command_id(record) in applied:
                continue
            if record.get("unit") == rt["unit"] and record.get("command") in ("reset", "reset-calls"):
                deferred.append(record)
                continue
            applied.append(command_id(record))
            del applied[:-APPLIED_KEEP]
            apply_one(cfg, state, record)
        save_state(cfg, state)
        requeue_commands(cfg, deferred)
        path.unlink(missing_ok=True)  # 套用結果已存檔，才刪認領檔


def apply_pending_commands(cfg, state):
    """挑單位之前先套用佇列裡的指令（呼叫端必須持有執行鎖）。
    否則「hours 結束後人才核准」時：approve 排進佇列，但等待核准的單位不算可執行，run／hours 只說
    「沒有可執行的單位」就結束，指令永遠不會被套用。這時沒有單位在跑，reset 也不必延後。"""
    if state is None:
        return state
    apply_queued({"cfg": cfg, "state": state, "unit": None})
    return state


def unapplied_commands(cfg):
    """佇列（含認領檔）裡還沒套用的指令筆數；status 顯示用，唯讀。"""
    state = load_state(cfg) or {}
    applied = set(state.get("applied_commands") or [])
    paths = [commands_path(cfg), *sorted(cfg.run_dir.glob("commands.processing-*.jsonl"))]
    ids = {command_id(r) for path in paths for r in read_jsonl(path)}
    return len(ids - applied)


def apply_one(cfg, state, record):
    command, unit = record.get("command"), record.get("unit")
    if not record.get("ancestry_checked"):
        # 不是經由 CLI（已做祖先鏈檢查）排入的：可能是引擎底下的程序直接寫佇列檔，不套用並發 ALERT
        reason = f"可疑的佇列指令未套用（沒有提交者檢查紀錄）：{command} {unit}（PID {record.get('pid')}）"
        add_alert(cfg, unit or "-", "suspicious", reason)
        log_event(cfg, state, unit or "-", reason)
        return
    try:
        if command not in MUTATIONS or unit not in state["units"]:
            raise RoundError(f"無法套用 {command} {unit}")
        print(f"    （套用佇列指令）{MUTATIONS[command](cfg, state, unit)}", flush=True)
    except RoundError as exc:
        log_event(cfg, state, unit or "-", f"佇列指令未套用：{exc}")


# ---------- 執行一輪 ----------


def maybe_fake_interrupt(station_id):
    """測試鉤子：Windows 很難從腳本對子程序送真正的 Ctrl+C，用這個模擬。"""
    if os.environ.get("LH_FAKE_INTERRUPT_AFTER") == station_id:
        print(f"    （測試鉤子）站 {station_id} 完成後模擬 Ctrl+C", flush=True)
        raise KeyboardInterrupt


def run_stations(rt, summary):
    """依 rec["station"] 一站一站走；退回會把 station 改回 producer，迴圈自然回頭。"""
    while True:
        apply_queued(rt)
        rec = stations.unit_rec(rt)
        if rec["status"] in FINAL_STATUSES or rec["status"] == PENDING_APPROVAL:
            return
        if stop.requested(rt["cfg"]):
            raise StopRequested(f"收到停止要求（stop），停在站 {rec['station']} 之前")
        station = rt["cfg"].station(rec["station"])
        print(f"[站 {stations.label_of(station)}]", flush=True)
        heartbeat(rt, f"站 {station['id']} 開始", 0)
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


def print_nothing_runnable(cfg, state):
    """沒有可跑的單位時說明原因：等待核准、依賴未完成，提示下一步該做什麼。"""
    print("queue 裡沒有可執行的工作單位")
    for unit in state["queue"]:
        if state["units"][unit]["status"] not in FINAL_STATUSES:
            reason = blocked_reason(cfg, state, unit)
            if reason:
                print(f"  - {reason}")


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
    rec = stations.unit_rec(rt)
    cost = rec["cost_usd"]
    retries = "　".join(f"{name} {count} 次" for name, count in rec["retries"].items()) or "無退回"
    print(chr(10) + "=" * 56)
    print(f"本輪摘要：第 {rec['round']} 輪｜{rt['unit']}｜狀態 {rec['status']}（站 {rec['station']}）")
    for label, result, seconds in summary:
        print(f"  站 {label:<14} {result}（{seconds:.1f} 秒）")
    print(f"  claude -p 次數：{rec['calls']}／{rt['cfg'].limits['calls_per_unit']}　退回：{retries}")
    print(f"  等值成本：US$ {cost:.4f}（約 NT$ {cost * USD_TO_TWD:.1f}，走 Max 訂閱不實扣）")
    print(f"  本次耗時：{elapsed:.1f} 秒")
    print("=" * 56, flush=True)


def outcome_of(status):
    if status == "done":
        return "成功", EXIT_OK
    if status == PENDING_APPROVAL:
        return "等待核准", EXIT_OK
    return "失敗", EXIT_UNIT_FAILED


def cmd_run(cfg, unit_arg=None):
    with lock.holding(cfg, "run"):
        integrity.clear_foreign_marker(cfg)  # 持鎖且還沒開站：任何既有標記都是當掉留下的
        standalone = not os.environ.get(lock.TOKEN_ENV)
        if standalone:
            stop.clear_request(cfg, "清除上次留下的停止要求")
        try:
            return run_round(cfg, unit_arg)
        finally:
            if standalone:
                stop.clear_request(cfg)  # hours 底下的 run 不清：hours 要看到它才會停


def run_round(cfg, unit_arg):
    changed = integrity.check_baseline(cfg, "run")
    if changed:
        print(f"!! {changed}；已鎖存（run/CONTROL-CHANGED.json），確認安全後執行 ack-control", file=sys.stderr)
        return EXIT_CONTROL_CHANGED
    state = load_state(cfg)
    if state is None:
        raise RoundError("沒有 state.json，先執行 init")
    apply_pending_commands(cfg, state)
    unit = pick_unit(cfg, state, unit_arg)
    if unit is None:
        if not unit_arg:
            print_nothing_runnable(cfg, state)
        return EXIT_OK
    write_snapshot(cfg)  # gate 本輪只讀這份快照
    rt = begin_round(cfg, state, unit)
    return run_unit(rt)


def run_unit(rt):
    start, summary = time.monotonic(), []
    try:
        run_stations(rt, summary)
    except KeyboardInterrupt:
        return handle_interrupt(rt, summary, time.monotonic() - start)
    except StopRequested as exc:
        return handle_interrupt(rt, summary, time.monotonic() - start, reason=str(exc))
    except CallBudgetExceeded as exc:
        stations.finish(rt, "failed", f"停止本單位：{exc}", alert=True)
    except RoundError as exc:
        return handle_error(rt, summary, exc, time.monotonic() - start)
    elapsed = time.monotonic() - start
    outcome, code = outcome_of(stations.unit_rec(rt)["status"])
    heartbeat(rt, outcome, elapsed)
    print_summary(rt, summary, elapsed)
    if rt.get("control_changed"):
        print(f"!! {rt['control_changed']}", file=sys.stderr, flush=True)
        return EXIT_CONTROL_CHANGED
    return code


def handle_error(rt, summary, exc, elapsed):
    """引擎錯誤：單位停在原站下次重試；連續太多次就標 failed，避免卡在同一個單位燒時間。"""
    rec, cfg = stations.unit_rec(rt), rt["cfg"]
    rec["engine_errors"] = rec.get("engine_errors", 0) + 1
    log_event(
        cfg,
        rt["state"],
        rt["unit"],
        f"錯誤（停在站 {rec['station']}，第 {rec['engine_errors']} 次連續引擎錯誤）：{exc}",
    )
    if rec["engine_errors"] >= cfg.limits["max_engine_errors"]:
        stations.finish(
            rt, "failed", f"連續 {rec['engine_errors']} 次引擎錯誤，標 failed（最後一次：{exc}）", alert=True
        )
    heartbeat(rt, "引擎錯誤", elapsed)
    print_summary(rt, summary, elapsed)
    print(f"錯誤：{exc}", file=sys.stderr, flush=True)
    return EXIT_ENGINE_ERROR


def handle_interrupt(rt, summary, elapsed, reason=None):
    """進度在每站結束時就存好了，這裡只補一筆紀錄與 heartbeat。reason 有值＝stop 指令要求停止。"""
    station = stations.unit_rec(rt)["station"]
    try:
        log_event(rt["cfg"], rt["state"], rt["unit"], reason or f"使用者中斷（停在站 {station}）")
        heartbeat(rt, "中斷", elapsed)
    except OSError as exc:
        print(f"（中斷時寫紀錄失敗：{exc}）", file=sys.stderr)
    print_summary(rt, summary, elapsed)
    print(f"{chr(10)}⛔ 中斷！進度已存在 state.json（{rt['unit']} 停在站 {station}），再執行一次會從這一站接著做")
    return EXIT_INTERRUPTED


# ---------- 其他指令 ----------


def cmd_init(cfg, unit_ids, force):
    """沒給 id 就用 factory.yaml 的全部單位；id 必須在 units.ids 裡（依賴關係才有意義）。"""
    unit_ids = list(dict.fromkeys(unit_ids or cfg.unit_ids))
    unknown = [unit for unit in unit_ids if unit not in cfg.unit_ids]
    if unknown:
        raise RoundError(f"這些單位不在 factory.yaml 的 units.ids：{unknown}")
    guard_human(cfg, "init")
    with lock.holding(cfg, "init"):
        state = load_state(cfg) or new_state()
        state["queue"] = unit_ids
        for unit in unit_ids:
            if force or unit not in state["units"]:
                state["units"][unit] = new_unit(cfg)
        log_event(cfg, state, "-", f"建立 queue：{' '.join(unit_ids)}{'（--force 重設）' if force else ''}")
    print(f"queue：{' '.join(unit_ids)}")
    return EXIT_OK


def print_latch(cfg):
    latch = integrity.read_latch(cfg)
    if latch is None:
        return
    bar = "!" * 60
    print(f"{bar}\n!! 🔴 工作目錄控制檔被改動（run/CONTROL-CHANGED.json；上次結束碼 6），run／hours 會拒絕啟動")
    print(f"!! {integrity.latch_summary(latch)}")
    print(f"!! 查清楚是誰改的、確認安全後才執行：ack-control（看差異）→ ack-control --yes\n{bar}")


def cmd_status(cfg):
    print_latch(cfg)
    state = load_state(cfg)
    if state is None:
        print("沒有 state.json（尚未 init）")
        return EXIT_OK
    print(f"專案 {cfg.name}｜目前輪次：{state['round']}｜queue：{' '.join(state['queue']) or '（空）'}")
    for unit in state["queue"]:
        rec = state["units"][unit]
        retries = ",".join(f"{k}={v}" for k, v in rec["retries"].items()) or "-"
        note = blocked_reason(cfg, state, unit) if rec["status"] not in FINAL_STATUSES else ""
        print(
            f"  {unit:<12} {rec['status']:<16} 站 {rec['station']:<10} 輪 {rec['round'] or '-':<3} "
            f"呼叫 {rec['calls']:>2}  退回 {retries:<24} US$ {rec['cost_usd']:.4f}  {note or ''}"
        )
    print_status_tail(cfg, state)
    return EXIT_OK


def print_status_tail(cfg, state):
    print("最近 history：")
    for item in state["history"][-8:]:
        print(f"  {item['ts']} [第 {item['round']} 輪 {item['unit']}] {item['event']}")
    pending, alerts = read_jsonl(pending_approvals_path(cfg)), read_jsonl(alerts_path(cfg))
    queued = unapplied_commands(cfg)
    if queued:
        print(f"佇列有 {queued} 筆未套用指令（run/commands.jsonl；下次 run／hours 開始時套用）")
    if pending:
        print(f"等待核准：{len(pending)} 筆（{', '.join(sorted({p.get('unit', '?') for p in pending}))}）")
    for alert in alerts[-5:]:
        print(f"⚠ ALERT {alert['ts']} {alert['unit']} {alert['status']}：{alert['reason']}")
    owner = lock.held_by_other(cfg)
    if owner:
        print(f"執行中：PID {owner.get('pid')}（{owner.get('purpose')}，自 {owner.get('since')}）")
    heartbeat_path = run_path(cfg, "heartbeat.txt")
    if heartbeat_path.exists():
        print("heartbeat：" + heartbeat_path.read_text(encoding="utf-8").strip())


def describe_entry(path_text, base, snap):
    if path_text not in (base or {}):
        return "新增"
    if path_text not in snap:
        return "刪除"
    return "修改（危險 git 設定鍵）" if snap[path_text]["kind"] == "gitconfig" else "修改"


def cmd_ack_control(cfg, yes):
    """人看過控制檔的變動後解除鎖存：不加 --yes 只印差異；加 --yes 才把目前的控制檔存成新基準、刪除鎖存。
    這是人工指令（三層檢查），而且要拿執行鎖：引擎在跑時不可改基準。"""
    guard_human(cfg, "ack-control")
    with lock.holding(cfg, "ack-control"):
        latch, base = integrity.read_latch(cfg), integrity.load_baseline(cfg)
        snap = integrity.control_snapshot(cfg)
        base_entries = base if isinstance(base, dict) else {}
        changed = integrity.diff(base_entries, snap)
        if latch:
            print(f"鎖存紀錄：{integrity.latch_summary(latch)}")
        if base == "corrupt":
            print("控制檔基準讀不懂（可能被改動），以下每個檔都會列為新增")
        print(f"與基準相比變動 {len(changed)} 個檔：")
        for path_text in changed[:200]:
            print(f"  {describe_entry(path_text, base_entries, snap)}：{path_text}")
        if latch is None and not changed and base is not None:
            print("沒有需要確認的變動")
            return EXIT_OK
        if not yes:
            print("確認這些變動都是安全的（例如是你自己裝的 hook）之後，再執行 ack-control --yes")
            return EXIT_OK
        integrity.save_baseline(cfg, snap)
        integrity.latch_path(cfg).unlink(missing_ok=True)
        state = load_state(cfg)
        if state is not None:
            log_event(cfg, state, "-", f"人工確認控制檔變動（{len(changed)} 個檔），更新基準並解除鎖存（ack-control）")
        print("已更新控制檔基準並解除鎖存；可以重新啟動 run／hours")
    return EXIT_OK


def cmd_reset_calls(cfg, unit):
    return cmd_mutate(cfg, "reset-calls", unit)


def cmd_reset(cfg, unit):
    return cmd_mutate(cfg, "reset", unit)


def cmd_approve(cfg, unit):
    return cmd_mutate(cfg, "approve", unit)


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
    return EXIT_OK
