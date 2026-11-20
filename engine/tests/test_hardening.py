"""QA 回報後的強化項目：QA／審查不可有 Bash、判定檔自動清除、state 欄位驗證、
hours 的 ALERT 與結束碼、等待核准的提示、Bash 安全邊界。"""

import copy
import json
import os
import subprocess
import sys

import pytest

from conftest import ENGINE_DIR, demo_config, write_project
from lhf import claude_call, gate
from lhf import round as round_mod
from lhf.config import load_config
from lhf.errors import ConfigError, RoundError
from lhf.state import load_state


def load_with(tmp_path, mutate):
    data = copy.deepcopy(demo_config())
    mutate(data)
    return load_config(write_project(tmp_path, data))


# ---------- 1. 角色分離不可被 Bash 繞過 ----------


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda d: d["roles"]["qa"].update(tools=["Read", "Write", "Bash"], bash_allow=["^echo "]), "roles.qa.tools"),
        (
            lambda d: d["roles"]["rv"].update(tools=["Read", "Write", "Edit", "Bash"], bash_allow=["^ls "]),
            "roles.rv.tools",
        ),
        (lambda d: d["roles"]["qa"].update(bash_allow=["^echo "]), "roles.qa.bash_allow"),
        (lambda d: d["roles"]["maker"].update(bash_allow=[]), "roles.maker.bash_allow"),
        (lambda d: d["roles"]["maker"].update(bash_allow=[".*"]), "^ 錨定"),
        (lambda d: d["roles"]["maker"].update(bash_allow=["^.*$"]), "太寬"),
        (lambda d: d["roles"]["maker"].update(bash_allow=["^.+"]), "太寬"),
        (lambda d: d["roles"]["maker"].update(bash_allow=["echo"]), "^ 錨定"),
    ],
)
def test_bash_policy_rejected(tmp_path, mutate, fragment):
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, mutate)
    assert fragment in str(info.value)


def test_checker_write_tools_still_bound_by_write(tmp_path, monkeypatch):
    """QA 有 Edit 也只能寫自己的報告位置。"""
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(tmp_path / "w"))
    cfg = load_with(tmp_path, lambda d: d["roles"]["qa"].update(tools=["Read", "Write", "Edit"]))
    ctx = {"role": "qa", "unit": "a", "round": "1", "station": "", "session": "s"}
    for tool in ("Edit", "Write"):
        event = {"tool_name": tool, "tool_input": {"file_path": str(tmp_path / "w" / "a.txt")}}
        assert gate.decide(event, cfg, ctx)[0] == "deny"
    event = {"tool_name": "Edit", "tool_input": {"file_path": str(cfg.run_dir / "qa" / "a.md")}}
    assert gate.decide(event, cfg, ctx)[0] == "allow"


# ---------- 2. 判定檔每次執行前由引擎清除（不依賴 clear） ----------

STALE_CLAUDE = r"""
import json, os, sys
from pathlib import Path
sys.stdin.read()
project, unit, role = Path(os.environ["LH_PROJECT_DIR"]), os.environ["LH_UNIT"], os.environ["LH_ROLE"]
run = project / "run"
with open(run / "trace.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"kind": "decision", "decision": "allow", "role": role, "unit": unit}) + "\n")
def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
marker = run / f"seen-{role}"
first = not marker.exists()
write(marker, "x")
if role == "maker":
    write(Path(os.environ["LH_WORK_OVERRIDE"]) / f"{unit}.txt", "x")
elif role == "qa" and first:
    write(run / "qa" / f"{unit}.md", "ok\nVERDICT: PASS\n")   # 第二次故意不寫判定檔
elif role == "rv":
    write(run / "rv" / f"{unit}.md", "退回\nVERDICT: REJECT\n" if first else "ok\nVERDICT: PASS\n")
print(json.dumps({"result": "ok", "session_id": "s", "total_cost_usd": 0, "num_turns": 1}))
"""


def test_stale_verdict_not_reused_without_clear(tmp_path, monkeypatch):
    data = copy.deepcopy(demo_config())
    for station in data["stations"]:
        station.pop("clear", None)
    project = write_project(tmp_path, data)
    script = tmp_path / "stale_claude.py"
    script.write_text(STALE_CLAUDE, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    monkeypatch.setattr(claude_call, "find_claude", lambda: [sys.executable, str(script)])
    cfg = load_config(project)
    round_mod.cmd_init(cfg, ["a"], False)
    round_mod.cmd_run(cfg)
    rec = load_state(cfg)["units"]["a"]
    qa_verdicts = [v["verdict"] for v in rec["verdicts"] if v["station"] == "qa"]
    assert qa_verdicts[:2] == ["PASS", "FAIL"]  # 第二次沒寫檔 → FAIL，不沿用舊 PASS
    assert "找不到判定檔" in [v for v in rec["verdicts"] if v["station"] == "qa"][1]["note"]


# ---------- 3. state.json 欄位驗證 ----------


@pytest.mark.parametrize(
    "content, fragment",
    [
        ({"round": 1, "queue": ["a"], "units": {"a": {"status": "in_progress"}}, "history": []}, "缺少欄位"),
        ({"round": 1, "queue": ["a"], "units": {}, "history": []}, "不在 units"),
        ({"round": "1", "queue": [], "units": {}, "history": []}, "round"),
    ],
)
def test_state_missing_fields_rejected(demo_dir, content, fragment):
    cfg = load_config(demo_dir)
    path = cfg.run_dir / "state.json"
    path.parent.mkdir(parents=True)
    raw = json.dumps(content)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(RoundError, match=fragment):
        round_mod.cmd_run(cfg)
    assert path.read_text(encoding="utf-8") == raw


def test_state_unknown_station_rejected(demo_dir, fake_mode):
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    path = cfg.run_dir / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["units"]["a"]["station"] = "renamed"
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(RoundError, match="不在 factory.yaml"):
        load_state(cfg)


def run_cli(args, env_extra=None):
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ENGINE_DIR), **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "lhf", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=ENGINE_DIR,
        check=False,
        timeout=300,
        stdin=subprocess.DEVNULL,
    )


def test_cli_state_missing_fields_exit_nonzero(demo_dir):
    path = demo_dir / "run" / "state.json"
    path.parent.mkdir()
    path.write_text('{"round":1,"queue":["a"],"units":{"a":{"status":"in_progress"}},"history":[]}', encoding="utf-8")
    proc = run_cli([str(demo_dir), "status"])
    assert proc.returncode == 1 and "Traceback" not in proc.stderr and "缺少欄位" in proc.stderr


# ---------- 4／5. hours 的 ALERT 與結束碼 ----------


def test_hours_corrupt_state_alerts(demo_dir):
    path = demo_dir / "run" / "state.json"
    path.parent.mkdir()
    path.write_text("{broken", encoding="utf-8")
    proc = run_cli([str(demo_dir), "hours", "--hours", "1", "--gap-seconds", "0"], {"LH_FAKE_MODEL": "1"})
    assert proc.returncode == 2 and "state.json 讀不到" in proc.stdout
    assert path.read_text(encoding="utf-8") == "{broken"
    assert "state.json 讀不到" in (demo_dir / "run" / "ALERT.txt").read_text(encoding="utf-8")


def test_hours_rejected_unit_alerts(demo_dir):
    env = {"LH_FAKE_MODEL": "1", "LH_FAKE_VERDICT": "FAIL_ALWAYS:rv"}
    run_cli([str(demo_dir), "init", "a"], env)
    proc = run_cli([str(demo_dir), "hours", "--hours", "1", "--max-rounds", "3", "--gap-seconds", "0"], env)
    assert proc.returncode == 1
    alert = (demo_dir / "run" / "ALERT.txt").read_text(encoding="utf-8")
    assert "a：rejected" in alert
    summary = next((demo_dir / "run" / "logs").glob("hours-summary-*.md")).read_text(encoding="utf-8")
    assert "rejected" in summary and "ALERT 清單（本次單位）：1" in summary


def test_hours_clean_run_exit_zero_no_alert(demo_dir):
    run_cli([str(demo_dir), "init", "a"], {"LH_FAKE_MODEL": "1"})
    proc = run_cli([str(demo_dir), "hours", "--hours", "1", "--gap-seconds", "0"], {"LH_FAKE_MODEL": "1"})
    assert proc.returncode == 0 and not (demo_dir / "run" / "ALERT.txt").exists()


# ---------- 6. 等待核准時的提示 ----------


def test_run_hints_pending_approval(demo_dir, fake_mode, monkeypatch, capsys):
    monkeypatch.setenv("LH_FAKE_APPROVAL_REQUEST", "maker")
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    round_mod.cmd_run(cfg)
    capsys.readouterr()
    assert round_mod.cmd_run(cfg) == 0
    assert "a 等待核准，執行 approve a" in capsys.readouterr().out


# ---------- 7. Bash 安全邊界 ----------


@pytest.fixture
def bash_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(tmp_path / "w"))

    def mutate(data):
        data["roles"]["maker"]["bash_allow"] = ["^echo ", "^cat ", "^git ", "^rm -f ", "^printenv"]

    return load_with(tmp_path, mutate)


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git  push",
        "GIT PUSH",
        "git\tpush",
        "rm -rf /tmp/x",
        "RM -RF x",
        "echo $ANTHROPIC_API_KEY",
        "printenv | grep ANTHROPIC",
        "echo x > protected/src/a.py",
        "cat protected\\src\\a.py",
        "cat protected/s''rc/a.py",
        'cat "PROTECTED/SRC/a.py"',
        "echo ok; ls",
        "echo `id`",
        "echo a && ls",
        "echo a\nls",
    ],
)
def test_bash_variants_denied(bash_cfg, command):
    ctx = {"role": "maker", "unit": "a", "round": "1", "station": "", "session": "s"}
    assert gate.decide({"tool_name": "Bash", "tool_input": {"command": command}}, bash_cfg, ctx)[0] == "deny"


def test_bash_plain_command_allowed(bash_cfg):
    ctx = {"role": "maker", "unit": "a", "round": "1", "station": "", "session": "s"}
    assert gate.decide({"tool_name": "Bash", "tool_input": {"command": "echo hello"}}, bash_cfg, ctx)[0] == "allow"
