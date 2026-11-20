"""gate 的 allow／deny 決策，以及以子程序執行時的 fail closed 行為。"""

import json
import subprocess
import sys

import pytest

from conftest import ENGINE_DIR
from lhf import gate
from lhf.config import load_config
from lhf.state import append_jsonl, approved_path, pending_approvals_path, read_jsonl, trace_path


@pytest.fixture
def k143(k143_dir, tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "practice" / "interactive").mkdir(parents=True)
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    return load_config(k143_dir), work


def ctx(role, unit="ch04", round_no="1", session="s1"):
    return {"role": role, "unit": unit, "round": round_no, "station": "", "session": session}


def write_event(path, tool="Write"):
    return {"tool_name": tool, "tool_input": {"file_path": str(path), "content": "x"}}


def bash_event(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def test_allow_planner_writes_plan(k143):
    cfg, _ = k143
    decision, reason, _ = gate.decide(write_event(cfg.run_dir / "plans" / "ch04.md"), cfg, ctx("planner"))
    assert decision == "allow", reason


def test_deny_tool_not_in_contract(k143):
    cfg, _ = k143
    decision, reason, _ = gate.decide(write_event(cfg.run_dir / "plans" / "ch04.md", "Edit"), cfg, ctx("planner"))
    assert decision == "deny" and "合約不允許 Edit" in reason


def test_deny_writer_other_unit(k143):
    cfg, work = k143
    target = work / "practice" / "interactive" / "ch05.sh"
    assert gate.decide(write_event(target), cfg, ctx("writer"))[0] == "deny"
    assert gate.decide(write_event(work / "practice" / "interactive" / "ch04.sh"), cfg, ctx("writer"))[0] == "allow"


def test_deny_unit_scoped_path_without_unit(k143):
    cfg, work = k143
    decision, reason, _ = gate.decide(
        write_event(work / "practice" / "interactive" / "ch04.sh"), cfg, ctx("writer", unit="")
    )
    assert decision == "deny" and "不可寫入" in reason


def test_deny_forbidden_path_case_and_backslash(k143):
    cfg, work = k143
    raw = str(work / "PRACTICE" / "Fable5" / "x.py").replace("/", "\\")
    decision, reason, _ = gate.decide(write_event(raw), cfg, ctx("writer"))
    assert decision == "deny" and "全域禁止寫入" in reason


def test_deny_relative_path_traversal(k143):
    cfg, work = k143
    event = {
        "tool_name": "Write",
        "cwd": str(work / "practice" / "interactive"),
        "tool_input": {"file_path": "../fable5/a.py"},
    }
    assert gate.decide(event, cfg, ctx("writer"))[1].startswith("全域禁止寫入")


def test_deny_unknown_role(k143):
    cfg, _ = k143
    decision, reason, _ = gate.decide(write_event(cfg.run_dir / "plans" / "ch04.md"), cfg, ctx("hacker"))
    assert decision == "deny" and "未知角色" in reason


def test_deny_budget_exceeded(k143):
    cfg, _ = k143
    for _ in range(15):
        append_jsonl(
            trace_path(cfg),
            {"kind": "decision", "decision": "allow", "role": "tester", "round": "1", "unit": "ch04", "session": "s1"},
        )
    target = cfg.run_dir / "test-logs" / "ch04.md"
    decision, reason, _ = gate.decide(write_event(target), cfg, ctx("tester"))
    assert decision == "deny" and "預算用完" in reason
    assert gate.decide(write_event(target), cfg, ctx("tester", session="s2"))[0] == "allow"  # 新呼叫有新額度


def test_missing_tool_name_raises(k143):
    cfg, _ = k143
    with pytest.raises(ValueError):
        gate.decide({}, cfg, ctx("planner"))


def test_bash_rules(demo_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(tmp_path / "w"))
    cfg = load_config(demo_dir)
    assert gate.decide(bash_event("echo hi"), cfg, ctx("maker", unit="a"))[0] == "allow"
    decision, reason, _ = gate.decide(bash_event("ls"), cfg, ctx("maker", unit="a"))
    assert decision == "deny" and "bash_allow" in reason
    decision, reason, _ = gate.decide(bash_event("echo x && GIT PUSH"), cfg, ctx("maker", unit="a"))
    assert decision == "deny" and "全域禁止指令" in reason
    assert "合約不允許 Bash" in gate.decide(bash_event("echo hi"), cfg, ctx("qa", unit="a"))[1]


def test_approval_command_then_approved(demo_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(tmp_path / "w"))
    data_roles = load_config(demo_dir).roles
    assert "^echo " in data_roles["maker"]["bash_allow"]
    cfg = load_config(demo_dir)
    decision, reason, approval = gate.decide(bash_event("ssh pi5 reboot"), cfg, ctx("maker", unit="a"))
    assert decision == "deny" and approval and "人工核准" in reason
    append_jsonl(approved_path(cfg), {"unit": "a", "round": "1", "command": "ssh pi5 reboot"})
    assert gate.decide(bash_event("ssh pi5 reboot"), cfg, ctx("maker", unit="a"))[0] == "allow"
    assert gate.decide(bash_event("ssh pi5 reboot"), cfg, ctx("maker", unit="b"))[2] is True  # 別的單位不共用
    assert gate.decide(bash_event("ssh pi5 reboot"), cfg, ctx("maker", unit="a", round_no="2"))[2] is True  # 別輪不共用
    gate.consume_approval(cfg, ctx("maker", unit="a"), "ssh pi5 reboot")
    assert gate.decide(bash_event("ssh pi5 reboot"), cfg, ctx("maker", unit="a"))[2] is True  # 用一次就失效


def test_approval_command_must_match_bash_allow_first(demo_dir, tmp_path, monkeypatch):
    """需核准的指令若不在白名單，直接拒絕，不進入核准流程（不會產生 pending）。"""
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(tmp_path / "w"))
    cfg = load_config(demo_dir)
    cfg.data["approval_commands"] = ["^scp "]
    decision, reason, approval = gate.decide(bash_event("scp a b"), cfg, ctx("maker", unit="a"))
    assert decision == "deny" and not approval and "bash_allow" in reason


def test_unit_with_traversal_is_ignored(monkeypatch):
    monkeypatch.setenv("LH_UNIT", "../ch04")
    assert gate.run_context({})["unit"] == ""
    monkeypatch.delenv("LH_UNIT")
    assert gate.run_context({"agent_type": "x"})["role"] == "x"


def run_gate(args, stdin, env_extra=None):
    import os

    env = {**os.environ, "PYTHONUTF8": "1", **(env_extra or {})}
    proc = subprocess.run(
        [sys.executable, str(ENGINE_DIR / "lhf" / "gate.py"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    return proc


def decision_of(proc):
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]


def test_subprocess_allow_and_trace(k143):
    cfg, _ = k143
    event = json.dumps(write_event(cfg.run_dir / "plans" / "ch04.md"))
    proc = run_gate(
        ["--project", str(cfg.project_dir)], event, {"LH_ROLE": "planner", "LH_UNIT": "ch04", "LH_ROUND": "1"}
    )
    assert proc.returncode == 0 and decision_of(proc) == "allow"
    assert read_jsonl(trace_path(cfg))[-1]["decision"] == "allow"


def test_subprocess_bad_json_fails_closed(k143):
    cfg, _ = k143
    proc = run_gate(["--project", str(cfg.project_dir)], "not json")
    assert proc.returncode == 0 and decision_of(proc) == "deny"
    assert "內部錯誤" in read_jsonl(trace_path(cfg))[-1]["reason"]


def test_subprocess_bad_project_fails_closed(tmp_path):
    proc = run_gate(["--project", str(tmp_path / "nope")], json.dumps(bash_event("echo")))
    assert proc.returncode == 0 and decision_of(proc) == "deny"


def test_subprocess_no_project_fails_closed():
    proc = run_gate([], json.dumps(bash_event("echo")))
    assert proc.returncode == 0 and decision_of(proc) == "deny"


def test_subprocess_approval_writes_pending(demo_dir, tmp_path):
    cfg = load_config(demo_dir)
    proc = run_gate(
        ["--project", str(demo_dir)],
        json.dumps(bash_event("ssh pi5")),
        {
            "LH_ROLE": "maker",
            "LH_UNIT": "a",
            "LH_ROUND": "1",
            "LH_STATION": "make",
            "LH_WORK_OVERRIDE": str(tmp_path / "w"),
        },
    )
    assert decision_of(proc) == "deny"
    pending = read_jsonl(pending_approvals_path(cfg))
    assert pending[-1]["command"] == "ssh pi5" and pending[-1]["station"] == "make"


def test_subprocess_post_records_result(k143):
    cfg, _ = k143
    event = {"tool_name": "Write", "tool_input": {}, "tool_response": {"error": "boom"}}
    proc = run_gate(
        ["--project", str(cfg.project_dir), "--post"], json.dumps(event), {"LH_ROLE": "planner", "LH_UNIT": "ch04"}
    )
    assert proc.returncode == 0 and proc.stdout == ""
    last = read_jsonl(trace_path(cfg))[-1]
    assert last["kind"] == "result" and last["decision"] == "failure"
