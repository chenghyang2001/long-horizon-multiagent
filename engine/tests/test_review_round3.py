"""code-qa 第三輪（282b00b）退回項目：撰寫者腳本自我核准（Z01）、引擎呼叫繞過（N50／N51）、WebFetch 網址強化。"""

import copy
import json
import os
import subprocess
import sys

import pytest

from conftest import ENGINE_DIR, PY, demo_config, write_project
from lhf import ancestry, gate, lock
from lhf import round as round_mod
from lhf.common import IN_ENGINE_ENV
from lhf.config import load_config
from lhf.errors import ConfigError, EngineDescendant
from lhf.state import alerts_path, append_jsonl, commands_path, load_state, read_jsonl

ATTACK = """
import os, sys
sys.path.insert(0, {engine!r})
if {strip_flag}:
    os.environ.pop({flag!r}, None)  # 模擬 env -u 清掉旗標
from lhf.__main__ import main
try:
    rc = main([{project!r}, "approve", "u1"])
except SystemExit as exc:
    rc = exc.code
print("RC=", rc)
"""


def attack_project(tmp_path, strip_flag):
    """u2 的 verify 指令站跑一支腳本，嘗試核准正在等待核准的 u1。"""
    script = tmp_path / "attack.py"
    data = copy.deepcopy(demo_config())
    data["units"]["ids"] = ["u1", "u2"]
    data["stations"].insert(4, {"id": "pub", "kind": "approve", "chain": "finish"})
    data["stations"][1]["cmds"] = [{"title": "攻擊", "cmd": f'"{PY}" "{script.as_posix()}"'}]
    project = write_project(tmp_path, data)
    script.write_text(
        ATTACK.format(engine=str(ENGINE_DIR), strip_flag=strip_flag, flag=IN_ENGINE_ENV, project=str(project)),
        encoding="utf-8",
    )
    return load_config(project)


def put_u1_on_hold(cfg):
    round_mod.cmd_init(cfg, ["u1", "u2"], False)
    state = load_state(cfg)
    rec = state["units"]["u1"]
    rec.update(status="pending_approval", station="pub", approval={"station": "pub", "ts": "x", "reason": "fixture"})
    (cfg.run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("strip_flag", [False, True])
def test_producer_script_cannot_self_approve(tmp_path, monkeypatch, strip_flag):
    """Z01：旗標在時被旗標擋；旗標被清掉（env -u）時被祖先鏈擋。兩種都不能核准、也不會排進佇列。"""
    monkeypatch.setenv("LH_FAKE_MODEL", "1")
    cfg = attack_project(tmp_path, strip_flag)
    put_u1_on_hold(cfg)
    round_mod.cmd_run(cfg, "u2")
    raw = (cfg.run_dir / "raw" / "u2.log").read_text(encoding="utf-8")
    assert "RC= 5" in raw, raw[-500:]
    assert ("祖先程序鏈" in raw) if strip_flag else (IN_ENGINE_ENV in raw)
    assert load_state(cfg)["units"]["u1"]["status"] == "pending_approval"
    assert not read_jsonl(commands_path(cfg))


def test_independent_terminal_approve_still_queues(tmp_path, monkeypatch):
    """從不是引擎子孫的程序（獨立終端）核准：照常排進佇列。"""
    monkeypatch.setenv("LH_FAKE_MODEL", "1")
    cfg = attack_project(tmp_path, False)
    put_u1_on_hold(cfg)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # 兄弟程序，不是祖先
    try:
        lock.lock_path(cfg).write_text(
            json.dumps(
                {"pid": holder.pid, "started": lock.process_start_time(holder.pid), "token": "t", "purpose": "hours"}
            ),
            encoding="utf-8",
        )
        assert round_mod.cmd_approve(cfg, "u1") == 0
    finally:
        holder.kill()
    queued = read_jsonl(commands_path(cfg))
    assert queued[0]["command"] == "approve" and queued[0]["ancestry_checked"] is True


def test_guard_detects_lock_holder_ancestor(demo_dir, monkeypatch):
    """持鎖的 PID 是自己的祖先（例如 pytest 的父程序鏈）＝引擎底下的程序。"""
    cfg = load_config(demo_dir)
    parent = ancestry.ancestors()[0]
    lock.lock_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    lock.lock_path(cfg).write_text(
        json.dumps({"pid": parent, "started": lock.process_start_time(parent), "token": "t"}), encoding="utf-8"
    )
    with pytest.raises(EngineDescendant, match="祖先程序鏈"):
        round_mod.guard_human(cfg, "approve")


def test_flag_alone_refuses(demo_dir, monkeypatch):
    monkeypatch.setenv(IN_ENGINE_ENV, "1")
    with pytest.raises(EngineDescendant, match=IN_ENGINE_ENV):
        round_mod.cmd_reset(load_config(demo_dir), "a")


def test_init_and_migrate_refused_inside_engine(demo_dir, monkeypatch, tmp_path):
    monkeypatch.setenv(IN_ENGINE_ENV, "1")
    cfg = load_config(demo_dir)
    with pytest.raises(EngineDescendant):
        round_mod.cmd_init(cfg, ["a"], False)
    from lhf import legacy

    with pytest.raises(EngineDescendant):
        legacy.cmd_migrate(cfg, tmp_path / "x.json", False)


def test_engine_children_carry_flag(demo_dir):
    from lhf import claude_call

    cfg = load_config(demo_dir)
    rt = {"cfg": cfg, "state": {}, "unit": "a", "round": 1}
    assert claude_call.command_env(cfg, rt)[IN_ENGINE_ENV] == "1"
    assert claude_call.build_env(cfg, rt, role="maker")[IN_ENGINE_ENV] == "1"
    assert not [k for k in claude_call.command_env(cfg, rt) if k.startswith("LH_")]  # 旗標不是 LH_ 開頭


def test_queued_command_without_check_is_not_applied(tmp_path, monkeypatch):
    """有人直接寫佇列檔（沒經過 CLI 的祖先檢查）：不套用，並寫 ALERT。"""
    monkeypatch.setenv("LH_FAKE_MODEL", "1")
    cfg = attack_project(tmp_path, False)
    put_u1_on_hold(cfg)
    append_jsonl(commands_path(cfg), {"id": "forged", "command": "approve", "unit": "u1", "pid": 1})
    state = load_state(cfg)
    round_mod.apply_queued({"cfg": cfg, "state": state, "unit": "u2", "round": 1})
    assert load_state(cfg)["units"]["u1"]["status"] == "pending_approval"
    alert = read_jsonl(alerts_path(cfg))[-1]
    assert alert["status"] == "suspicious" and "可疑" in alert["reason"]


def test_cli_exit_code_refused(demo_dir):
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ENGINE_DIR), IN_ENGINE_ENV: "1"}
    result = subprocess.run(
        [sys.executable, "-m", "lhf", str(demo_dir), "reset-calls", "a"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ENGINE_DIR,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert result.returncode == 5 and "獨立的終端機" in result.stderr


# ---------- N50／N51：引擎呼叫繞過 ----------

ENGINE = ENGINE_DIR.as_posix()
MUST_DENY = [
    "python -m lhf proj approve ch04",
    "python -m  lhf x",
    "python -mlhf proj approve ch04",
    "py -m lhf x",
    "python\t-m\tlhf x",
    "python -m LHF x",
    "python -m 'lhf' x",
    'python -m "lhf" x',
    "python -m l''hf x",
    "python -m　lhf x",
    f"python {ENGINE}/lhf/__main__.py x",
    f"python {ENGINE.upper()}/LHF/__MAIN__.PY x",
    "python engine\\lhf\\gate.py",
    "python ../engine/lhf/__main__.py",
    "echo hi; python -m lhf x",
    "python -m lhf",
    "python -m lhf.gate",
    "python -Im lhf x",
    "python -X utf8 -m lhf x",
    "python -W ignore -m lhf x",
    'python -c "import lhf"',
    'python -c "from lhf.__main__ import main"',
    'PYTHON -C "IMPORT LHF"',
    'python -c "from  LHF import round"',
    "python -c \"import importlib;importlib.import_module('l'+'hf')\"",
    "python -m runpy lhf",
    "python -mrunpy lhf",
    'python -c "__import__(chr(108)+chr(104)+chr(102))"',
]


@pytest.fixture
def bash_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(tmp_path / "w"))
    data = copy.deepcopy(demo_config())
    data["roles"]["maker"]["bash_allow"] += ["^python ", "^py ", "^pytest "]
    return load_config(write_project(tmp_path, data))


def bash(cfg, command):
    event = {"tool_name": "Bash", "tool_input": {"command": command}}
    return gate.decide(
        event, cfg, {"role": "maker", "unit": "a", "round": "1", "station": "", "session": "s", "trace_offset": 0}
    )


@pytest.mark.parametrize("command", MUST_DENY)
def test_engine_invocation_variants_denied(bash_cfg, command):
    assert bash(bash_cfg, command)[0] == "deny"


@pytest.mark.parametrize("command", ["echo hi", "python -c print(1)", "pytest -m slow tests", "python tools/run.py"])
def test_normal_commands_still_allowed(bash_cfg, command):
    assert bash(bash_cfg, command)[0] == "allow"


# ---------- WebFetch 網址 ----------


@pytest.fixture
def fetch_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(tmp_path / "w"))
    data = copy.deepcopy(demo_config())
    data["roles"]["maker"]["tools"].append("WebFetch")
    data["roles"]["maker"]["fetch_allow"] = ["^https://docs[.]example[.]com/"]
    return load_config(write_project(tmp_path, data))


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://docs.example.com/page", "allow"),
        ("https://docs.example.com.evil.net/x", "deny"),
        ("https://docs.example.com@evil.net/", "deny"),
        (" https://docs.example.com/x", "deny"),
        ("https://docs.example.com/x\nhttps://evil", "deny"),
        ("https://docs.example.com/x\tq", "deny"),
        ("https://docs.example.com/a b", "deny"),
        ("https://docs.example.com/\x00", "deny"),
        ("https://docs.example.com/\x7f", "deny"),
        ("file:///etc/passwd", "deny"),
    ],
)
def test_webfetch_url_rules(fetch_cfg, url, expected):
    event = {"tool_name": "WebFetch", "tool_input": {"url": url, "prompt": "x"}}
    ctx = {"role": "maker", "unit": "a", "round": "1", "station": "", "session": "s", "trace_offset": 0}
    assert gate.decide(event, fetch_cfg, ctx)[0] == expected


@pytest.mark.parametrize(
    "pattern, ok",
    [
        (r"^https://docs\.example\.com/", True),
        (r"^https://docs[.]example[.]com/", True),
        (r"^https://docs\.example\.com$", True),
        (r"^https://docs\.example\.com(/|$)", True),
        (r"^https?://docs\.example\.com/", True),
        (r"^https://docs\.example\.com", False),
        (r"^https://docs\.example\.com.*", False),
        (r"^https://docs\.example\.com\.", False),
    ],
)
def test_fetch_allow_requires_host_boundary(tmp_path, pattern, ok):
    data = copy.deepcopy(demo_config())
    data["roles"]["maker"]["tools"].append("WebFetch")
    data["roles"]["maker"]["fetch_allow"] = [pattern]
    project = write_project(tmp_path, data)
    if ok:
        load_config(project)
    else:
        with pytest.raises(ConfigError, match="邊界|太寬"):
            load_config(project)
