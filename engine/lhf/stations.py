"""五種通用站型：agent／parallel／command／commit／approve，以及判定與退回規則。

rt（runtime）＝{"cfg", "state", "unit", "round"}。每站做完都經 advance()／apply_fail() 存檔，
所以任何時候中斷，下一次執行都會從 rec["station"] 那一站接著做。
退回一律回到 producer 站（config 已驗證），之後必然重新經過 verify＋qa 才能再進 review。
"""

import re
import shlex
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lhf import claude_call
from lhf.config import DEFAULT_FEEDBACK, is_fake, posix, render, render_path
from lhf.errors import CallBudgetExceeded, RoundError
from lhf.state import (
    IN_PROGRESS,
    PENDING_APPROVAL,
    add_alert,
    append_jsonl,
    file_offset,
    log_event,
    now_str,
    pending_approvals_path,
    read_jsonl_since,
    save_state,
    write_engine_trace,
)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]")
VERDICT_LINE_RE = re.compile(r"`?VERDICT:\s*([A-Z]+)`?")


# ---------- 共用 ----------


def unit_rec(rt):
    return rt["state"]["units"][rt["unit"]]


def mapping_of(rt, **extra):
    values = rt["cfg"].vars(unit=rt["unit"], round_no=rt["round"])
    values.update({key: str(value) for key, value in extra.items()})
    return values


def render_list(templates, mapping):
    return [render_path(item, mapping) for item in templates or []]


def event(rt, text):
    log_event(rt["cfg"], rt["state"], rt["unit"], text)


def label_of(station):
    return station.get("label") or station["id"]


def advance(rt, station, text):
    """本站成功：清掉以本站為目標的退回意見，前進到下一站；沒有下一站就是 done。"""
    rec = unit_rec(rt)
    if rec.get("feedback") and rec["feedback"].get("for") == station["id"]:
        rec["feedback"] = None
    next_id = rt["cfg"].next_station_id(station["id"])
    if next_id is None:
        rec["status"], rec["finished"] = "done", now_str()
    else:
        rec["status"], rec["station"] = IN_PROGRESS, next_id
    event(rt, text)


def finish(rt, status, text, alert=False):
    rec = unit_rec(rt)
    rec["status"], rec["finished"] = status, now_str()
    if alert:
        add_alert(rt["cfg"], rt["unit"], status, text)
    event(rt, text)


def reserve_calls(rt, count):
    """呼叫前先扣次數並存檔：就算呼叫中途當掉也算一次，防止反覆重試無上限。"""
    rec, limit = unit_rec(rt), rt["cfg"].limits["calls_per_unit"]
    if rec["calls"] + count > limit:
        raise CallBudgetExceeded(f"本單位 claude -p 已用 {rec['calls']} 次，再呼叫 {count} 次會超過上限 {limit}")
    rec["calls"] += count
    save_state(rt["cfg"], rt["state"])


def record_call(rt, role, info):
    rec = unit_rec(rt)
    rec["cost_usd"] = round(rec["cost_usd"] + info["cost"], 6)
    rec["sessions"].append(
        {"role": role, "session_id": info["session_id"], "cost_usd": info["cost"], "turns": info["turns"]}
    )
    print(f"    ✔ {role} 完成（{info['turns']} 輪對話、US$ {info['cost']:.4f}）", flush=True)


# ---------- prompt ----------


def feedback_text(rt):
    """退回時把上一版沒過的原因整段附上（只取檔尾），撰寫者才知道要修什麼。"""
    feedback = unit_rec(rt).get("feedback")
    if not feedback:
        return ""
    path, limit = Path(feedback["path"]), rt["cfg"].limits["feedback_max_chars"]
    try:
        content = path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError as exc:
        content = f"（讀不到 {posix(path)}：{exc}）"
    template = rt["cfg"].data.get("feedback_template") or DEFAULT_FEEDBACK
    return render(
        template, mapping_of(rt, feedback_label=feedback["label"], feedback_path=posix(path), feedback_content=content)
    )


def extract_values(rt, mapping):
    """extracts：從檔案用 regex 抓一段放進 prompt（例如對照表中本單位那一列）。"""
    values = {}
    escaped = {key: re.escape(value) for key, value in mapping.items()}
    for name, spec in (rt["cfg"].data.get("extracts") or {}).items():
        try:
            text = Path(render_path(spec["file"], mapping)).read_text(encoding="utf-8")
        except OSError:
            text = ""
        match = re.search(render(spec["regex"], escaped), text, re.MULTILINE) if text else None
        values[name] = match.group(0) if match else spec.get("default", "")
    return values


def prompt_text(rt, prompt_name):
    mapping = mapping_of(rt)
    mapping.update(extract_values(rt, mapping))
    mapping["feedback"] = feedback_text(rt)
    return render(rt["cfg"].data["prompts"][prompt_name], mapping)


# ---------- 判定與退回 ----------


def read_verdict(path, words):
    """讀最後一個非空行的 VERDICT；缺檔、空檔、格式不對、值不合法一律當不通過（fail closed）。"""
    passed, failed = words
    try:
        lines = [
            line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()
        ]
    except OSError:
        return failed, f"找不到判定檔 {path.name}"
    match = VERDICT_LINE_RE.fullmatch(lines[-1]) if lines else None
    if match and match.group(1) in words:
        return match.group(1), ""
    return failed, f"{path.name} 最後一行不是合法 VERDICT（只接受 {'／'.join(words)}）"


def record_verdict(rt, station, verdict, note):
    unit_rec(rt)["verdicts"].append({"ts": now_str(), "station": station["id"], "verdict": verdict, "note": note})
    write_engine_trace(rt["cfg"], rt, "verdict", verdict, f"{station['id']}：{note}".rstrip("："))


def apply_fail(rt, station, verdict, note, feedback_path):
    """退回 producer 站；超過上限就標 failed／rejected、寫 ALERT 清單，下一輪不會自動重做。"""
    rule, rec = rt["cfg"].on_fail(station), unit_rec(rt)
    record_verdict(rt, station, verdict, note)
    used = rec["retries"].get(rule["counter"], 0)
    if used >= rule["max"]:
        text = f"{label_of(station)} {verdict}，已退回 {used} 次（上限 {rule['max']}）仍不過 {note}".strip()
        finish(rt, rule["status"], text, alert=True)
        return verdict
    rec["retries"][rule["counter"]] = used + 1
    rec["feedback"] = {
        "station": station["id"],
        "label": rule["label"],
        "path": str(feedback_path),
        "for": rule["goto"],
    }
    rec["status"], rec["station"] = IN_PROGRESS, rule["goto"]
    write_engine_trace(rt["cfg"], rt, "rework", rule["goto"], f"{station['id']} {verdict} → 退回 {rule['goto']}")
    event(
        rt,
        f"{label_of(station)} {verdict} {note}→ 退回 {rule['goto']}（{rule['counter']} 第 {used + 1} 次）".replace(
            "  ", " "
        ),
    )
    return verdict


def apply_verdict(rt, station, verdict, note, report_path):
    passed, _ = rt["cfg"].verdict_words(station)
    if verdict == passed:
        record_verdict(rt, station, verdict, note)
        advance(rt, station, f"{label_of(station)} {verdict}")
        return verdict
    return apply_fail(rt, station, verdict, note, report_path)


# ---------- 核准 ----------


def new_approvals(rt, offset):
    return [r for r in read_jsonl_since(pending_approvals_path(rt["cfg"]), offset) if r.get("unit") == rt["unit"]]


def mark_pending_approval(rt, station, reason):
    rec = unit_rec(rt)
    rec["status"], rec["station"] = PENDING_APPROVAL, station["id"]
    rec["approval"] = {"station": station["id"], "ts": now_str(), "reason": reason}
    event(rt, f"等待人工核准（站 {station['id']}）：{reason}；確認後執行 approve {rt['unit']}")
    return "等待核准"


# ---------- agent／parallel ----------


def check_requires(rt, templates, mapping):
    """requires 只在真模式檢查：假模式的工作目錄是空的暫存區。"""
    if is_fake():
        return
    missing = [path for path in render_list(templates, mapping) if not Path(path).exists()]
    if missing:
        raise RoundError(f"找不到必要的輸入檔：{'、'.join(missing)}")


def prepare_paths(station, mapping, jobs=()):
    """建好產出檔的資料夾，刪掉 clear 清單（避免代理沒寫檔卻讀到上一版的 PASS）。"""
    targets = render_list(station.get("outputs"), mapping)
    for job in jobs:
        targets += render_list(job.get("outputs"), mapping)
    if station.get("verdict"):
        targets.append(render_path(station["verdict"]["file"], mapping))
    for path in targets + render_list(station.get("clear"), mapping):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    for path in render_list(station.get("mkdirs"), mapping):
        Path(path).mkdir(parents=True, exist_ok=True)
    for path in render_list(station.get("clear"), mapping):
        Path(path).unlink(missing_ok=True)


def missing_outputs(templates, mapping):
    return [Path(path).name for path in render_list(templates, mapping) if not Path(path).exists()]


def make_job(rt, role, prompt_name, outputs):
    return {"role": role, "prompt_text": prompt_text(rt, prompt_name), "outputs": outputs or []}


def station_agent(rt, station):
    mapping = mapping_of(rt)
    check_requires(rt, station.get("requires"), mapping)
    prepare_paths(station, mapping)
    job = make_job(rt, station["role"], station["prompt"], station.get("outputs"))
    reserve_calls(rt, 1)
    offset = file_offset(pending_approvals_path(rt["cfg"]))
    record_call(rt, station["role"], claude_call.call_agent(rt["cfg"], rt, station, job))
    requests = new_approvals(rt, offset)
    if requests:
        return mark_pending_approval(
            rt, station, f"{station['role']} 要執行需核准的指令 {requests[-1].get('command')!r}"
        )
    missing = missing_outputs(station.get("outputs"), mapping)
    if missing:
        raise RoundError(f"{station['role']} 沒有寫出 {'、'.join(missing)}")
    if station.get("verdict"):
        report = Path(render_path(station["verdict"]["file"], mapping))
        verdict, note = read_verdict(report, rt["cfg"].verdict_words(station))
        return apply_verdict(rt, station, verdict, note, report)
    advance(rt, station, f"{label_of(station)} 完成")
    return "完成"


def run_parallel(rt, station, jobs):
    """平行跑；全部等完才回，成功的結果不因另一個失敗而丟掉。"""
    results, errors = {}, {}
    pool = ThreadPoolExecutor(max_workers=len(jobs))
    try:
        futures = {job["role"]: pool.submit(claude_call.call_agent, rt["cfg"], rt, station, job) for job in jobs}
        for role, future in futures.items():
            try:
                results[role] = future.result()
            except RoundError as exc:
                errors[role] = str(exc)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results, errors


def collect_parallel(rt, specs, results, errors, mapping):
    """記錄成功的呼叫；必要產出缺漏算錯誤；once 工作成功就記下，重做時不再跑。"""
    rec = unit_rec(rt)
    for spec in specs:
        role = spec["role"]
        if role not in results:
            continue
        record_call(rt, role, results[role])
        missing = missing_outputs(spec.get("outputs"), mapping)
        if missing:
            errors[role] = f"沒有寫出 {'、'.join(missing)}"
        elif spec.get("once") and role not in rec["once_done"]:
            rec["once_done"].append(role)


def station_parallel(rt, station):
    rec, mapping = unit_rec(rt), mapping_of(rt)
    check_requires(rt, station.get("requires"), mapping)
    specs = [spec for spec in station["jobs"] if not (spec.get("once") and spec["role"] in rec["once_done"])]
    prepare_paths(station, mapping, specs)
    if not specs:
        advance(rt, station, f"{label_of(station)} 無需重做（只做一次的工作都已完成）")
        return "略過"
    jobs = [make_job(rt, spec["role"], spec["prompt"], spec.get("outputs")) for spec in specs]
    reserve_calls(rt, len(jobs))
    offset = file_offset(pending_approvals_path(rt["cfg"]))
    results, errors = run_parallel(rt, station, jobs)
    collect_parallel(rt, specs, results, errors, mapping)
    requests = new_approvals(rt, offset)
    if requests:
        return mark_pending_approval(rt, station, f"{requests[-1].get('role')} 要執行需核准的指令")
    if errors:
        save_state(rt["cfg"], rt["state"])
        raise RoundError(f"站 {station['id']} 失敗：" + "；".join(f"{role}：{msg}" for role, msg in errors.items()))
    advance(rt, station, f"{label_of(station)} 完成（{'、'.join(results)}）")
    return "＋".join(results)


# ---------- command ----------


def run_one(argv, cwd, env, timeout):
    """回傳 (exit code 或「逾時」／「無法執行」, 秒數, 去掉 ANSI 色碼的輸出)。"""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        code, output = "逾時", claude_call.as_text(exc.stdout) + claude_call.as_text(exc.stderr)
    except OSError as exc:
        code, output = "無法執行", str(exc)
    return code, time.monotonic() - start, ANSI_RE.sub("", output)


def format_block(title, argv, code, elapsed, output):
    return (
        f"===== {title} =====\n指令：{shlex.join(argv)}\nexit code：{code}　耗時：{elapsed:.1f} 秒\n"
        f"----- 輸出 -----\n{output.rstrip()}\n"
    )


def expand_cmds(station, mapping):
    """把 cmds 展開成 (標題, argv, fail_on_error)；steps 是閉區間 [起, 迄]。"""
    expanded = []
    for item in station["cmds"]:
        first, last = item.get("steps") or [None, None]
        steps = [None] if first is None else range(first, last + 1)
        for step in steps:
            values = {**mapping, "step": "" if step is None else str(step)}
            argv = shlex.split(render(item["cmd"], values))
            if not argv:
                raise RoundError(f"站 {station['id']} 有空的指令")
            expanded.append((render(item.get("title") or item["cmd"], values), argv, item.get("fail_on_error", False)))
    return expanded


def resolve_exe(argv):
    exe = shutil.which(argv[0])
    if exe is None:
        raise RoundError(f"找不到執行檔 {argv[0]}")
    return [exe, *argv[1:]]


def run_checks(rt, station, mapping):
    """依序執行；fail_on_error 的指令失敗就停並自動判失敗。回傳 (raw log, 自動失敗原因或空字串)。"""
    cwd = render_path(station.get("cwd") or "{WORK}", mapping)
    timeout = station.get("timeout") or rt["cfg"].limits["command_timeout"]
    env = claude_call.build_env(rt["cfg"], rt, station_id=station["id"])
    blocks = []
    for title, argv, fail_on_error in expand_cmds(station, mapping):
        print(f"    執行：{title}", flush=True)
        code, elapsed, output = run_one(resolve_exe(argv), cwd, env, timeout)
        blocks.append(format_block(title, argv, code, elapsed, output))
        if fail_on_error and code != 0:
            return "\n".join(blocks), f"{title}失敗：{output.strip()[:300]}"
    return "\n".join(blocks), ""


def station_command(rt, station):
    """客觀驗證：程式自己跑，不靠模型說；必要檔缺漏或 fail_on_error 失敗直接退回，省一次 QA 呼叫。"""
    mapping = mapping_of(rt)
    missing = missing_outputs(station.get("require"), mapping)
    if missing:
        raw_text, auto_fail = f"缺檔：{'、'.join(missing)}\n", f"缺少必要檔案 {'、'.join(missing)}"
    else:
        raw_text, auto_fail = run_checks(rt, station, mapping)
    log_path, report = Path(render_path(station["log"], mapping)), Path(render_path(station["fail_report"], mapping))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"# {rt['unit']} 執行紀錄　{now_str()}\n\n{raw_text}", encoding="utf-8")
    if auto_fail:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            f"# {rt['unit']} 自動判定（站 {station['id']}）\n\n{auto_fail}\n\n原始紀錄：{posix(log_path)}\n\n"
            "VERDICT: FAIL\n",
            encoding="utf-8",
        )
        apply_fail(rt, station, "FAIL", f"（站 {station['id']} 自動）", report)
        return "FAIL（自動：" + auto_fail[:40] + "）"
    advance(rt, station, f"{label_of(station)} 完成 → {log_path.name}")
    return "執行完成"


# ---------- commit／approve ----------


def git(work, args):
    try:
        return subprocess.run(
            ["git", "-C", str(work), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise RoundError(f"無法執行 git：{exc}") from exc


def check_branch(cfg, work):
    expected = cfg.data.get("branch")
    if not expected:
        return
    current = git(work, ["rev-parse", "--abbrev-ref", "HEAD"])
    if current.returncode != 0 or current.stdout.strip() != expected:
        raise RoundError(f"工作目錄分支是 {current.stdout.strip() or '?'}，不是設定的 {expected}，拒絕 commit")


def station_commit(rt, station):
    """只 commit 指定檔（git commit -- <paths>），不 push；假模式或 override 一律不碰真的工作目錄。"""
    cfg = rt["cfg"]
    if cfg.is_override():
        print("    （假模式／override）略過 commit", flush=True)
        advance(rt, station, "完成（假模式略過 commit）")
        return "略過"
    work, mapping = cfg.work_dir(), mapping_of(rt)
    check_branch(cfg, work)
    files = render_list(station["paths"], mapping)
    added = git(work, ["add", "--", *files])
    if added.returncode != 0:
        raise RoundError(f"git add 失敗：{added.stderr.strip()[:300]}")
    if git(work, ["diff", "--cached", "--quiet", "--", *files]).returncode == 0:
        advance(rt, station, "完成（檔案沒有變更，略過 commit）")
        return "無變更"
    message = render(station.get("message") or "工廠：{unit}（第 {round} 輪）", mapping)
    committed = git(work, ["commit", "-m", message, "--", *files])
    if committed.returncode != 0:
        raise RoundError(f"git commit 失敗：{(committed.stderr or committed.stdout).strip()[:300]}")
    advance(rt, station, f"完成並 commit：{message}")
    return "已 commit"


def station_approve(rt, station):
    """第一次到站：寫 pending 並停下；approve 之後再到站就放行（核准用一次就失效）。"""
    rec = unit_rec(rt)
    if station["id"] in rec["approved"]:
        rec["approved"].remove(station["id"])
        advance(rt, station, f"{label_of(station)} 已人工核准，繼續")
        return "已核准"
    message = render(station.get("message") or "站 {unit} 需要人工核准", mapping_of(rt))
    append_jsonl(
        pending_approvals_path(rt["cfg"]),
        {
            "ts": now_str(),
            "unit": rt["unit"],
            "round": str(rt["round"]),
            "role": "",
            "station": station["id"],
            "kind": "station",
            "command": message,
        },
    )
    return mark_pending_approval(rt, station, message)


STATION_FUNCS = {
    "agent": station_agent,
    "parallel": station_parallel,
    "command": station_command,
    "commit": station_commit,
    "approve": station_approve,
}
