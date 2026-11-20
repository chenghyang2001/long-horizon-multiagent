"""claude -p 呼叫層：找執行檔、命令列、環境變數，以及用假 claude 驗證 gate 生效檢查（fail closed）。"""

import json
import os
import sys
from pathlib import Path

import pytest

from lhf import claude_call
from lhf import round as round_mod
from lhf.config import load_config
from lhf.errors import RoundError
from lhf.state import load_state


@pytest.mark.skipif(os.name != "nt", reason="claude.exe 優先順序只在 Windows 有意義")
def test_find_claude_prefers_native_exe(tmp_path, monkeypatch):
    monkeypatch.delenv("LH_CLAUDE_BIN", raising=False)  # 測 find_claude 本身：拿掉測試安全網
    exe = tmp_path / ".local" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert claude_call.find_claude() == [str(exe)]


NPM_SHIM = (
    '@ECHO off\r\nSETLOCAL\r\nIF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n'
    '  SET "_prog=node"\r\n)\r\n'
    'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
    '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n'
)


def test_find_claude_cmd_switches_to_node(tmp_path, monkeypatch):
    monkeypatch.delenv("LH_CLAUDE_BIN", raising=False)  # 測 find_claude 本身：拿掉測試安全網
    cmd = tmp_path / "claude.CMD"
    cmd.write_text(NPM_SHIM, encoding="utf-8")
    cli = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-home"))  # 沒有 ~/.local/bin/claude.exe
    monkeypatch.setattr(
        claude_call.shutil, "which", lambda name: {"claude": str(cmd), "node": "/usr/bin/node"}.get(name)
    )
    assert claude_call.find_claude() == ["/usr/bin/node", str(cli.resolve())]


def test_find_claude_unparsable_shim_errors(tmp_path, monkeypatch):
    """reviewer 的 npmsim：shim 只有 @echo off、看不出目標 → 清楚報錯，不退回去執行 .CMD。"""
    monkeypatch.delenv("LH_CLAUDE_BIN", raising=False)  # 測 find_claude 本身：拿掉測試安全網
    cmd = tmp_path / "claude.cmd"
    cmd.write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-home"))
    monkeypatch.setattr(claude_call.shutil, "which", lambda name: {"claude": str(cmd)}.get(name))
    with pytest.raises(RoundError, match="看不出它實際執行哪個檔"):
        claude_call.find_claude()


def test_shim_pointing_to_exe(tmp_path, monkeypatch):
    monkeypatch.delenv("LH_CLAUDE_BIN", raising=False)  # 測 find_claude 本身：拿掉測試安全網
    exe = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    cmd = tmp_path / "claude.cmd"
    cmd.write_text('"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe" %*', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-home"))
    monkeypatch.setattr(claude_call.shutil, "which", lambda name: {"claude": str(cmd)}.get(name))
    assert claude_call.find_claude() == [str(exe.resolve())]


def test_find_claude_override(monkeypatch):
    monkeypatch.setenv("LH_CLAUDE_BIN", "/x/claude")
    assert claude_call.find_claude() == ["/x/claude"]


def test_ensure_single_line_args():
    with pytest.raises(RoundError, match="換行"):
        claude_call.ensure_single_line_args(["claude", "a\nb"])


def rt_of(cfg, unit="ch04"):
    return {"cfg": cfg, "state": {}, "unit": unit, "round": 3}


def test_build_cmd_variadic_tools(k143_dir):
    cfg = load_config(k143_dir)
    cmd = claude_call.describe_cmd(cfg, rt_of(cfg), "writer")
    index = cmd.index("--tools")
    assert cmd[index + 1 : index + 6] == ["Read", "Glob", "Grep", "Write", "Edit"] and cmd[index + 6] == "--settings"
    assert "--append-system-prompt-file" in cmd and cmd[cmd.index("--setting-sources") + 1] == "project,local"
    assert cmd[cmd.index("--permission-mode") + 1] == "default"
    assert all("~" not in arg for arg in cmd)


def test_build_env_strips_secrets(k143_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", "keep")
    cfg = load_config(k143_dir)
    env = claude_call.build_env(cfg, rt_of(cfg), role="writer", station_id="write")
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["CLAUDE_CODE_GIT_BASH_PATH"] == "keep"
    assert (env["LH_ROLE"], env["LH_UNIT"], env["LH_ROUND"], env["LH_STATION"]) == ("writer", "ch04", "3", "write")
    assert env["K143_CLI_MAX_CALLS"] == "6" and env["LH_PROJECT_DIR"] == str(cfg.project_dir)


FAKE_CLAUDE = r"""
import json, os, sys
from pathlib import Path
prompt = sys.stdin.read()
log = Path(os.environ["FAKE_LOG"])
log.write_text(json.dumps({"argv": sys.argv[1:], "has_key": "ANTHROPIC_API_KEY" in os.environ,
                           "role": os.environ.get("LH_ROLE"), "prompt_len": len(prompt)}), encoding="utf-8")
if os.environ.get("FAKE_WRITE_TRACE") == "1":
    trace = Path(os.environ["LH_PROJECT_DIR"]) / "run" / "trace.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    with open(trace, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "decision", "decision": "allow", "role": os.environ["LH_ROLE"],
                             "unit": os.environ["LH_UNIT"]}) + "\n")
    out = Path(os.environ["FAKE_OUT"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("x", encoding="utf-8")
print(json.dumps({"result": "ok", "session_id": "s", "total_cost_usd": 0.01, "num_turns": 2}))
"""


@pytest.fixture
def real_mode_demo(demo_dir, tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-be-removed")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "call.json"))
    monkeypatch.setenv("FAKE_OUT", str(work / "a.txt"))
    monkeypatch.setattr(claude_call, "find_claude", lambda: [sys.executable, str(script)])
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    return cfg, tmp_path


def test_gate_not_active_fails_closed(real_mode_demo, capsys):
    cfg, tmp_path = real_mode_demo
    assert round_mod.cmd_run(cfg) == 3  # 引擎錯誤
    rec = load_state(cfg)["units"]["a"]
    assert rec["station"] == "make" and rec["calls"] == 1 and rec["engine_errors"] == 1  # 結果不採用，停在原站
    assert "gate 沒有生效" in capsys.readouterr().err
    call = json.loads((tmp_path / "call.json").read_text(encoding="utf-8"))
    assert call["has_key"] is False and call["role"] == "maker" and call["prompt_len"] > 0


def test_gate_active_station_advances(real_mode_demo, monkeypatch):
    cfg, _ = real_mode_demo
    monkeypatch.setenv("FAKE_WRITE_TRACE", "1")
    monkeypatch.setenv("LH_FAKE_INTERRUPT_AFTER", "make")
    assert round_mod.cmd_run(cfg) == 130
    rec = load_state(cfg)["units"]["a"]
    assert rec["station"] == "check" and rec["cost_usd"] == 0.01


def test_non_json_output_saves_failure_log(real_mode_demo, tmp_path, monkeypatch):
    cfg, _ = real_mode_demo
    script = tmp_path / "bad.py"
    script.write_text("print('plain text')\n", encoding="utf-8")
    monkeypatch.setattr(claude_call, "find_claude", lambda: [sys.executable, str(script)])
    assert round_mod.cmd_run(cfg) == 3
    logs = list((cfg.run_dir / "logs").glob("agent-fail-*-maker.txt"))
    assert logs and "plain text" in logs[0].read_text(encoding="utf-8")


def test_hook_settings_points_to_gate(k143_dir):
    cfg = load_config(k143_dir)
    settings = json.loads(claude_call.write_hook_settings(cfg).read_text(encoding="utf-8"))
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "lhf/gate.py" in command and "--project" in command
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 30
    assert settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"].endswith("--post")
