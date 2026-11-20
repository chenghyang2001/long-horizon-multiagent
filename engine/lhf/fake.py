"""假模型模式（LH_FAKE_MODEL=1）：不呼叫 claude，依站型產生輸出，給引擎回歸測試用。

假模式一樣走完整鐵律鏈（producer → verify → qa → review → finish），只是代理換成假函式。
測試鉤子：
  LH_FAKE_VERDICT=FAIL_ONCE[:<站>]    該 verdict 站（預設第一個 verdict 站＝qa）每單位第一次判失敗
  LH_FAKE_VERDICT=FAIL_ALWAYS[:<站>]  該 verdict 站永遠判失敗（測退回上限）
  LH_FAKE_APPROVAL_REQUEST=<角色>     模擬 gate 擋下需核准的指令（寫一筆 pending）
  LH_FAKE_DELAY_SEC=<秒>             每次假呼叫先等幾秒（測併行：引擎跑一半時從外面下指令）
"""

import os
import time
from datetime import datetime
from pathlib import Path

from lhf.config import render, render_path
from lhf.state import append_jsonl, now_str, pending_approvals_path, run_path


def write_file(path_text, content):
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def fake_files(cfg, rt, station, job, mapping):
    """要寫的檔：fake 區段指定的內容 ＋ 必要產出（預設一行假文字）。"""
    files = {}
    for path_tpl, content_tpl in ((cfg.data.get("fake") or {}).get(job["role"]) or {}).items():
        files[render_path(path_tpl, mapping)] = render(content_tpl, mapping)
    for path_tpl in job.get("outputs") or []:
        files.setdefault(render_path(path_tpl, mapping), f"（假）{job['role']} 為 {rt['unit']} 產出\n")
    return files


def verdict_mode(cfg, station):
    """解析 LH_FAKE_VERDICT，回傳 'once'／'always'／None（只對指定站生效）。"""
    raw = os.environ.get("LH_FAKE_VERDICT", "")
    mode, _, target = raw.partition(":")
    if mode not in ("FAIL_ONCE", "FAIL_ALWAYS"):
        return None
    if not target:
        target = next(s["id"] for s in cfg.stations if s.get("verdict"))
    if target != station["id"]:
        return None
    return "once" if mode == "FAIL_ONCE" else "always"


def fake_verdict(cfg, rt, station):
    """用標記檔記住「已經失敗過」，跨程序（中斷接續）也成立。"""
    passed, failed = cfg.verdict_words(station)
    mode = verdict_mode(cfg, station)
    if mode == "always":
        return failed
    if mode == "once":
        marker = run_path(cfg, ".runtime") / f"fake-fail-once-{station['id']}-{rt['unit']}"
        if not marker.exists():
            write_file(str(marker), "x")
            return failed
    return passed


def fake_approval_request(cfg, rt, station, role):
    if os.environ.get("LH_FAKE_APPROVAL_REQUEST") != role:
        return
    append_jsonl(
        pending_approvals_path(cfg),
        {
            "ts": now_str(),
            "unit": rt["unit"],
            "round": str(rt["round"]),
            "role": role,
            "station": station["id"],
            "kind": "command",
            "command": "（假）需要核准的指令",
        },
    )


def fake_agent(cfg, rt, station, job):
    delay = float(os.environ.get("LH_FAKE_DELAY_SEC") or 0)
    if delay > 0:
        time.sleep(delay)
    mapping = cfg.vars(unit=rt["unit"], round_no=rt["round"])
    for path_text, content in fake_files(cfg, rt, station, job, mapping).items():
        write_file(path_text, content)
    if station.get("verdict") and station["kind"] == "agent":
        verdict = fake_verdict(cfg, rt, station)
        target = render_path(station["verdict"]["file"], mapping)
        write_file(target, f"# {rt['unit']} 假判定（{station['id']}）\n\n假內容\n\nVERDICT: {verdict}\n")
    fake_approval_request(cfg, rt, station, job["role"])
    stamp = datetime.now().strftime("%H%M%S%f")
    return {"result": f"（假）{job['role']} 完成", "session_id": f"fake-{job['role']}-{stamp}", "cost": 0.0, "turns": 1}
