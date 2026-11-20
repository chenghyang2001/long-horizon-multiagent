"""長時程任務的中斷與接續＋人工核准 示範（stage2 2-4，只用標準庫）

用法（Git Bash，記得 PYTHONUTF8=1）：
  python run_task.py              從 state.json 接續；沒有 state 就從頭開始
  python run_task.py --approve    核准「寄出」報告（只在 pending_approval 時有效）
  python run_task.py --status     印出目前狀態摘要，不呼叫模型
  python run_task.py --reset      刪除 state.json、notes/、report.md、outbox/
  python run_task.py --pause 3    每個子題做完暫停 3 秒，方便示範 Ctrl+C

對照 K138 的概念：
  - state.json ≈ SqliteSaver 的 checkpoint：每個小步驟做完就存一次，
    崩潰或中斷後從最後一個 checkpoint 接著跑，只損失「正在做的那一步」。
  - 本目錄固定只有一個任務 ≈ 固定 thread_id：同一個 thread 再呼叫就是接續，
    --reset 等於換一個新的 thread_id。
  - send 階段要 --approve ≈ interrupt_on：對外動作（寄信）前先暫停等人核准，
    核准後才從暫停點繼續。

測試鉤子（只給冒煙測試用）：
  LH_FAKE_MODEL=1            不呼叫 claude，回傳假資料
  LH_FAKE_INTERRUPT_AFTER=n  本次執行完成 n 個子題後主動 raise KeyboardInterrupt；
                             因為 Windows 上很難從腳本對子程序送真正的 Ctrl+C，
                             用這個鉤子模擬「使用者按下 Ctrl+C」。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATE_PATH = BASE_DIR / "state.json"
NOTES_DIR = BASE_DIR / "notes"
REPORT_PATH = BASE_DIR / "report.md"
OUTBOX_DIR = BASE_DIR / "outbox"

TOPIC = "長時間（24 小時）跑多代理 AI 程式要注意的事"
STAGES = ["plan", "research", "report", "send"]
STAGE_NAMES = {"plan": "規劃子題", "research": "研究子題", "report": "合成報告", "send": "寄出報告"}
SUBTOPIC_COUNT = 3
MODEL_TIMEOUT_SEC = 180
APPROVE_HINT = "⏸ 報告準備寄出，需要核准：python run_task.py --approve"


class TaskError(Exception):
    """可預期的任務錯誤（模型呼叫失敗、解析失敗等），main 會印出並以 exit 1 結束。"""


# ---------- 狀態存取 ----------

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_state():
    """全新任務的初始狀態；欄位固定，--status 與接續邏輯都依賴這些鍵。"""
    return {
        "task": TOPIC,
        "stage": "plan",
        "status": "running",
        "subtopics": [],
        "done": [],
        "history": [],
        "session_ids": [],
    }


def load_state():
    """讀 state.json；不存在回 None。壞檔直接報錯，不默默從頭開始以免蓋掉進度。"""
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskError(f"state.json 讀取失敗（{STATE_PATH}）：{exc}") from exc


def save_state(state):
    """先寫 .tmp 再 os.replace：replace 是原子操作，中斷時不會留下寫一半的 state.json。"""
    tmp_path = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, STATE_PATH)


def record_step(state, stage, message):
    """記一筆 history 並立刻存檔——這就是 checkpoint，每個小步驟都要呼叫。"""
    state["history"].append({"time": now_str(), "stage": stage, "message": message})
    save_state(state)


# ---------- 呼叫模型 ----------

def fake_model(prompt):
    """LH_FAKE_MODEL=1 時的假模型：只看 prompt 是否要求 JSON 陣列來決定回傳格式。"""
    stamp = datetime.now().strftime("%H%M%S%f")
    if "JSON 陣列" in prompt:
        text = json.dumps(["假子題一：資源與成本控管", "假子題二：狀態保存與接續", "假子題三：監控與人工介入"],
                          ensure_ascii=False)
    else:
        text = "這是假模型的固定回覆。\n第二行。\n第三行。"
    return text, f"fake-{stamp}"


def call_model(prompt):
    """所有模型呼叫都集中在這裡，日後換模型只改這個函式。回傳 (text, session_id)。"""
    if os.environ.get("LH_FAKE_MODEL") == "1":
        return fake_model(prompt)
    claude_path = shutil.which("claude")
    if claude_path is None:
        raise TaskError("找不到 claude 執行檔（shutil.which('claude') 為 None），請確認已安裝並在 PATH 中")
    env = os.environ.copy()
    # 成本鐵律：有 ANTHROPIC_API_KEY 時 claude -p 會改扣 API Credits，移除後走 Max 訂閱
    env.pop("ANTHROPIC_API_KEY", None)
    cmd = [claude_path, "-p", "--output-format", "json", "--tools", "", "--setting-sources", "project,local"]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", timeout=MODEL_TIMEOUT_SEC, env=env, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TaskError(f"claude -p 逾時（超過 {MODEL_TIMEOUT_SEC} 秒）") from exc
    if proc.returncode != 0:
        raise TaskError(f"claude -p 結束碼 {proc.returncode}：{(proc.stderr or proc.stdout).strip()[:500]}")
    return parse_claude_output(proc.stdout)


def parse_claude_output(stdout):
    """解析 --output-format json 的輸出，取 result 與 session_id。"""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TaskError(f"claude -p 輸出不是 JSON：{stdout.strip()[:300]}") from exc
    if payload.get("is_error"):
        raise TaskError(f"claude -p 回報錯誤：{payload.get('result')}")
    result = payload.get("result")
    if result is None:
        raise TaskError(f"claude -p 輸出缺少 result 欄位：{stdout.strip()[:300]}")
    return result, payload.get("session_id", "")


def ask_model(state, purpose, prompt):
    """印出呼叫前後訊息與耗時，並把 session_id 記進 state（方便事後追查每次呼叫）。"""
    print(f"  → 呼叫 claude -p：{purpose}")
    start = time.monotonic()
    text, session_id = call_model(prompt)
    print(f"    完成，耗時 {time.monotonic() - start:.1f} 秒")
    state["session_ids"].append(session_id)
    return text


# ---------- 四個階段 ----------

def parse_subtopics(text):
    """先整段當 JSON 解析，失敗再用正規表示式抓第一個 [...]；都失敗就報錯，不自己編子題。"""
    candidates = [text.strip()]
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            items = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(items, list) and len(items) == SUBTOPIC_COUNT and all(isinstance(i, str) for i in items):
            return [i.strip() for i in items]
    raise TaskError(f"無法從模型回覆解析出 {SUBTOPIC_COUNT} 個子題：{text.strip()[:300]}")


def stage_plan(state):
    """把題目拆成 3 個子題；已拆過就略過（接續時不重複花錢呼叫模型）。"""
    if state["subtopics"]:
        print("  ↩ 子題已規劃（state.json 記錄），跳過")
        return
    prompt = (f"題目：「{TOPIC}」。請把這個題目拆成剛好 {SUBTOPIC_COUNT} 個子題（繁體中文）。"
              f"只回傳一個 JSON 陣列字串，例如 [\"子題一\", \"子題二\", \"子題三\"]，不要任何其他文字。")
    text = ask_model(state, "拆成 3 個子題", prompt)
    state["subtopics"] = parse_subtopics(text)
    for idx, sub in enumerate(state["subtopics"], start=1):
        print(f"    子題 {idx}：{sub}")
    record_step(state, "plan", f"拆出 {SUBTOPIC_COUNT} 個子題")


def research_one(state, idx, subtopic):
    """研究單一子題並立刻 checkpoint；中斷時最多只重做這一個子題。"""
    prompt = (f"總題目：「{TOPIC}」。子題：「{subtopic}」。"
              "請用繁體中文寫 3～5 行重點筆記，每行一個重點，以「- 」開頭，不要標題與其他說明。")
    text = ask_model(state, f"研究子題 {idx}", prompt)
    NOTES_DIR.mkdir(exist_ok=True)
    note_path = NOTES_DIR / f"sub-{idx}.md"
    note_path.write_text(f"# 子題 {idx}：{subtopic}\n\n{text.strip()}\n", encoding="utf-8")
    state["done"].append(idx)
    record_step(state, "research", f"完成子題 {idx}，寫入 notes/sub-{idx}.md")
    print(f"    ✔ 子題 {idx} 完成 → notes/sub-{idx}.md（{len(state['done'])}/{SUBTOPIC_COUNT}）")


def stage_research(state, pause_sec):
    """逐一研究尚未完成的子題；已完成的直接跳過，這就是「接續」。"""
    interrupt_after = read_fake_interrupt_after()
    finished_this_run = 0
    for idx, subtopic in enumerate(state["subtopics"], start=1):
        if idx in state["done"]:
            print(f"  ↩ 子題 {idx} 已完成（state.json 記錄），跳過")
            continue
        research_one(state, idx, subtopic)
        finished_this_run += 1
        if interrupt_after is not None and finished_this_run >= interrupt_after:
            # 測試鉤子：模擬使用者在這個時間點按下 Ctrl+C
            raise KeyboardInterrupt
        if pause_sec > 0:
            print(f"    （暫停 {pause_sec:g} 秒，可以按 Ctrl+C 試試中斷）")
            time.sleep(pause_sec)


def read_fake_interrupt_after():
    """讀 LH_FAKE_INTERRUPT_AFTER；格式錯誤要報錯，避免測試以為有中斷其實沒有。"""
    raw = os.environ.get("LH_FAKE_INTERRUPT_AFTER")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise TaskError(f"LH_FAKE_INTERRUPT_AFTER 必須是整數，收到：{raw!r}") from exc
    return value if value > 0 else None


def stage_report(state):
    """讀所有筆記合成報告；筆記缺檔代表狀態不一致，報錯而不是產出殘缺報告。"""
    sections = []
    for idx in range(1, len(state["subtopics"]) + 1):
        note_path = NOTES_DIR / f"sub-{idx}.md"
        if not note_path.exists():
            raise TaskError(f"找不到筆記 {note_path}，state 與檔案不一致，請 --reset 後重跑")
        sections.append(note_path.read_text(encoding="utf-8"))
    prompt = (f"以下是「{TOPIC}」的三份子題筆記。請用繁體中文合成一份約 8 行的 Markdown 報告，"
              "第一行是 # 標題，其餘為重點條列。只回報告本身。\n\n" + "\n\n".join(sections))
    text = ask_model(state, "合成報告", prompt)
    REPORT_PATH.write_text(text.strip() + "\n", encoding="utf-8")
    print("    ✔ 報告完成 → report.md")
    record_step(state, "report", "合成 report.md")


def stage_send(state):
    """寄出是對外動作，對照 interrupt_on：沒有核准就停在這裡，等 --approve。"""
    state["status"] = "pending_approval"
    record_step(state, "send", "等待人工核准寄出")
    print_approve_hint()


def print_approve_hint():
    bar = "=" * 56
    print(f"\n{bar}\n{APPROVE_HINT}\n{bar}")


# ---------- 指令 ----------

STAGE_FUNCS = {"plan": stage_plan, "report": stage_report, "send": stage_send}


def run_pipeline(state, pause_sec):
    """從 state["stage"] 指到的階段往後跑；做完一個階段就把指標推進並存檔。"""
    start_index = STAGES.index(state["stage"])
    for index in range(start_index, len(STAGES)):
        stage = STAGES[index]
        state["stage"] = stage
        print(f"[階段 {index + 1}/{len(STAGES)}] {STAGE_NAMES[stage]}")
        if stage == "research":
            stage_research(state, pause_sec)
        else:
            STAGE_FUNCS[stage](state)
        if stage != "send":
            state["stage"] = STAGES[index + 1]
            save_state(state)


def cmd_run(pause_sec):
    """無參數：依 status 決定是接續、只提醒核准，還是告知已完成。"""
    state = load_state()
    if state is None:
        print(f"沒有 state.json，從頭開始任務：{TOPIC}")
        state = new_state()
        record_step(state, "plan", "建立新任務")
    elif state["status"] == "done":
        print("任務已完成（報告已寄出到 outbox/sent-report.md）。要重來請 --reset")
        return
    elif state["status"] == "pending_approval":
        print_approve_hint()
        return
    else:
        print(f"從 state.json 接續：目前階段 {state['stage']}，已完成 {len(state['done'])}/{SUBTOPIC_COUNT} 個子題")
    try:
        run_pipeline(state, pause_sec)
    except KeyboardInterrupt:
        print(f"\n⛔ 中斷！進度已存在 state.json（已完成 {len(state['done'])}/{SUBTOPIC_COUNT} 個子題），"
              "再執行一次會從這裡接著做")
        sys.exit(130)


def cmd_approve():
    """模擬寄出：複製報告到 outbox 並加寄出時間。教學示範，絕不真的寄信。"""
    state = load_state()
    if state is None or state.get("status") != "pending_approval":
        current = "無 state.json" if state is None else state.get("status")
        print(f"--approve 只在 status 為 pending_approval 時有效（目前：{current}）。先執行 python run_task.py")
        return
    if not REPORT_PATH.exists():
        raise TaskError(f"找不到 {REPORT_PATH}，無法寄出")
    OUTBOX_DIR.mkdir(exist_ok=True)
    sent_path = OUTBOX_DIR / "sent-report.md"
    body = REPORT_PATH.read_text(encoding="utf-8")
    sent_path.write_text(f"<!-- 模擬寄出時間：{now_str()} -->\n{body}", encoding="utf-8")
    state["status"] = "done"
    record_step(state, "send", "人工核准，已模擬寄出到 outbox/sent-report.md")
    print("✔ 已核准，模擬寄出 → outbox/sent-report.md（未真的寄信）")


def cmd_status():
    """只讀 state 印摘要，不呼叫模型，也不改任何檔案。"""
    state = load_state()
    if state is None:
        print("沒有 state.json（尚未開始或已 --reset）")
        return
    print(f"任務：{state['task']}")
    print(f"階段：{state['stage']}　狀態：{state['status']}　模型呼叫次數：{len(state['session_ids'])}")
    print("子題：" + ("（尚未規劃）" if not state["subtopics"] else ""))
    for idx, sub in enumerate(state["subtopics"], start=1):
        mark = "✔" if idx in state["done"] else "…"
        print(f"  {mark} {idx}. {sub}")
    print("最近 history：")
    for item in state["history"][-5:]:
        print(f"  {item['time']} [{item['stage']}] {item['message']}")


def cmd_reset():
    """清掉所有產物，等於開一個新的 thread_id。"""
    for path in (STATE_PATH, STATE_PATH.with_name(STATE_PATH.name + ".tmp"), REPORT_PATH):
        if path.exists():
            path.unlink()
    for folder in (NOTES_DIR, OUTBOX_DIR):
        if folder.exists():
            shutil.rmtree(folder)
    print("已重設：刪除 state.json、notes/、report.md、outbox/")


def parse_args(argv):
    parser = argparse.ArgumentParser(description="長時程任務中斷接續＋人工核准示範")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--approve", action="store_true", help="核准寄出報告")
    group.add_argument("--status", action="store_true", help="印出狀態摘要，不呼叫模型")
    group.add_argument("--reset", action="store_true", help="刪除所有狀態與產物")
    parser.add_argument("--pause", type=float, default=0.0, help="每個子題做完暫停秒數（預設 0）")
    args = parser.parse_args(argv)
    if args.pause < 0:
        parser.error("--pause 不能是負數")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.reset:
            cmd_reset()
        elif args.status:
            cmd_status()
        elif args.approve:
            cmd_approve()
        else:
            cmd_run(args.pause)
    except TaskError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"檔案操作錯誤：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
