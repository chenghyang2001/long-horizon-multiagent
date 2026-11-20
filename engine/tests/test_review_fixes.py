"""code-reviewer 退回項目的回歸測試：程序樹逾時、執行鎖與指令佇列、內部檔保護與設定快照、
工具白名單、退回帶 raw log、舊進度轉移，以及 N1～N11、schema 預留。"""

import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ENGINE_DIR, PY, REPO_DIR, demo_config, write_project
from lhf import claude_call, common, gate, legacy, lock, proc, stations
from lhf import round as round_mod
from lhf.config import load_config
from lhf.errors import ConfigError, RoundError
from lhf.snapshot import load_for_gate, snapshot_path, write_snapshot
from lhf.state import alerts_path, append_jsonl, commands_path, load_state, read_jsonl, trace_path

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="需要 bash")


def project_with(tmp_path, mutate, name="demo"):
    data = copy.deepcopy(demo_config())
    mutate(data)
    return write_project(tmp_path, data, name)


def run_cli(args, env_extra=None, timeout=300):
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
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def sleep_count():
    if os.name != "nt":
        return 0
    out = subprocess.run(
        ["tasklist"], capture_output=True, text=True, encoding="cp950", errors="replace", stdin=subprocess.DEVNULL
    ).stdout
    return sum(1 for line in out.splitlines() if line.lower().startswith("sleep.exe"))


# ---------- MUST 1：逾時殺整棵程序樹 ----------


@needs_bash
def test_run_tree_kills_background_grandchild():
    before = sleep_count()
    start = time.monotonic()
    result = proc.run_tree([BASH, "-c", "echo start; sleep 25 & sleep 100"], cwd=".", env=os.environ.copy(), timeout=3)
    assert result.timed_out and "start" in result.stdout
    assert time.monotonic() - start < 15
    time.sleep(0.5)
    assert sleep_count() <= before  # 背景的 sleep 25 也被殺掉


@needs_bash
def test_command_station_timeout_returns_quickly(tmp_path, fake_mode, capsys):
    def mutate(data):
        data["stations"][1]["timeout"] = 3
        data["stations"][1]["cmds"] = [{"title": "掛住", "cmd": "bash -c 'sleep 25 & sleep 100'"}]
        data["stations"][1]["on_fail"] = {"goto": "make", "max": 0}
        data["stations"][2]["on_fail"] = {"goto": "make", "max": 0}

    cfg = load_config(project_with(tmp_path, mutate))
    round_mod.cmd_init(cfg, ["a"], False)
    start = time.monotonic()
    assert round_mod.cmd_run(cfg) == 1
    assert time.monotonic() - start < 20
    assert "exit code：逾時" in (cfg.run_dir / "raw" / "a.log").read_text(encoding="utf-8")


def test_stream_tree_timeout_and_output(tmp_path):
    seen = []
    script = "import time,sys; print('中文一行', flush=True); time.sleep(30)"
    code, status = proc.stream_tree(
        [sys.executable, "-c", script], cwd=str(tmp_path), env=os.environ.copy(), timeout=3, on_output=seen.append
    )
    assert status == "timeout" and code is None and "中文一行" in "".join(seen)


def test_round_time_limit_covers_all_attempts(k143_dir):
    cfg = load_config(k143_dir)
    # 10 次×900 秒 ＋ 9 個指令×180 秒×(1＋2 次退回＋1) ＋ 600
    assert cfg.round_time_limit() == 10 * 900 + 9 * 180 * 4 + 600


def test_heartbeat_written_at_station_start(demo_dir, fake_mode, monkeypatch):
    monkeypatch.setenv("LH_FAKE_INTERRUPT_AFTER", "make")
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    round_mod.cmd_run(cfg)
    monkeypatch.delenv("LH_FAKE_INTERRUPT_AFTER")
    monkeypatch.setenv("LH_FAKE_INTERRUPT_AFTER", "check")
    original = stations.STATION_FUNCS["command"]
    seen = []

    def spy(rt, station):
        seen.append((cfg.run_dir / "heartbeat.txt").read_text(encoding="utf-8"))
        return original(rt, station)

    monkeypatch.setitem(stations.STATION_FUNCS, "command", spy)
    round_mod.cmd_run(cfg)
    assert "站 check 開始" in seen[0]


# ---------- MUST 2：執行鎖＋外部指令佇列 ----------


def test_lock_blocks_second_run(demo_dir, fake_mode):
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        path = lock.lock_path(cfg)
        path.write_text(
            json.dumps(
                {"pid": holder.pid, "started": lock.process_start_time(holder.pid), "token": "x", "purpose": "hours"}
            ),
            encoding="utf-8",
        )
        assert run_cli([str(demo_dir), "run"], {"LH_FAKE_MODEL": "1"}).returncode == 4
    finally:
        holder.kill()


def test_stale_lock_is_cleared(demo_dir, fake_mode, capsys):
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    lock.lock_path(cfg).write_text(json.dumps({"pid": 999999, "started": 1, "token": "old"}), encoding="utf-8")
    assert round_mod.cmd_run(cfg) == 0
    assert not lock.lock_path(cfg).exists()


def test_reused_pid_with_other_start_time_is_stale():
    """PID 被別的程序重用時，啟動時間不同 → 視為殘留舊鎖。"""
    real_start = lock.process_start_time(os.getpid())
    if real_start in (0, None):
        pytest.skip("這個系統拿不到程序啟動時間")
    assert lock.lock_alive({"pid": os.getpid(), "started": real_start})
    assert not lock.lock_alive({"pid": os.getpid(), "started": real_start + 1})


def test_approve_queued_while_hours_runs_other_unit(tmp_path):
    """hours 跑 b 時 approve a：寫進佇列、由引擎套用，不會被 b 的存檔覆蓋。"""

    def mutate(data):
        data["stations"].insert(4, {"id": "publish", "kind": "approve", "chain": "finish"})
        data["units"]["ids"] = ["a", "b"]

    project = project_with(tmp_path, mutate)
    env = {"LH_FAKE_MODEL": "1"}
    run_cli([str(project), "init", "a", "b"], env)
    assert run_cli([str(project), "run", "--unit", "a"], env).returncode == 0  # a 停在 publish 等核准
    slow = subprocess.Popen(
        [sys.executable, "-m", "lhf", str(project), "hours", "--hours", "1", "--max-rounds", "1", "--gap-seconds", "0"],
        cwd=ENGINE_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONPATH": str(ENGINE_DIR),
            "LH_FAKE_MODEL": "1",
            "LH_FAKE_DELAY_SEC": "1.5",
        },
    )
    try:
        deadline = time.monotonic() + 30
        while not lock.lock_path(load_config(project)).exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(1)
        approved = run_cli([str(project), "approve", "a"], env)
        assert approved.returncode == 0 and "已排入" in approved.stdout
        slow.wait(timeout=120)
    finally:
        slow.kill()
    state = json.loads((project / "run" / "state.json").read_text(encoding="utf-8"))
    assert state["units"]["a"]["status"] == "in_progress" and state["units"]["a"]["approved"] == ["publish"]
    assert state["units"]["b"]["status"] == "pending_approval"  # b 也跑到 publish，兩邊都沒被覆蓋
    assert not commands_path(load_config(project)).exists() or not read_jsonl(commands_path(load_config(project)))


def test_reset_of_running_unit_is_deferred(demo_dir, fake_mode):
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    state = load_state(cfg)
    rt = {"cfg": cfg, "state": state, "unit": "a", "round": 1}
    append_jsonl(commands_path(cfg), {"command": "reset", "unit": "a"})
    round_mod.apply_queued(rt)
    assert read_jsonl(commands_path(cfg))[0]["command"] == "reset"


def test_tmp_files_include_pid(tmp_path):
    assert str(os.getpid()) in common.tmp_name(tmp_path / "state.json").name


# ---------- MUST 3：引擎內部檔保護＋設定快照 ----------


@pytest.mark.parametrize(
    "rel",
    [
        "factory.yaml",
        "agents/qa.md",
        "run/state.json",
        "run/approvals/approved.jsonl",
        "run/trace.jsonl",
        "run/alerts.jsonl",
        "run/commands.jsonl",
        "run/.runtime/hook-settings.json",
        "run/logs/x.txt",
    ],
)
def test_config_rejects_write_to_internal_files(tmp_path, rel):
    def mutate(data):
        data["roles"]["maker"]["write"] = ["{out}", "{PROJECT}/" + rel]

    with pytest.raises(ConfigError, match="引擎內部檔"):
        load_config(project_with(tmp_path, mutate))


def test_gate_denies_engine_internal_even_if_config_bypassed(demo_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(tmp_path / "w"))
    cfg = load_config(demo_dir)
    cfg.roles["maker"]["write"] = ["**"]  # 模擬設定驗證被繞過：gate 仍固定拒絕
    ctx = {"role": "maker", "unit": "a", "round": "1", "station": "", "session": "s"}
    for target in (demo_dir / "factory.yaml", demo_dir / "run" / "state.json", ENGINE_DIR / "lhf" / "gate.py"):
        event = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
        decision, reason, _ = gate.decide(event, cfg, ctx)
        assert decision == "deny" and "引擎內部檔" in reason


def test_config_rejects_write_overlapping_run_dir(tmp_path):
    with pytest.raises(ConfigError, match="引擎內部檔"):
        load_config(project_with(tmp_path, lambda d: d["roles"]["qa"].update(write=["{RUN}/**"])))


def test_gate_reads_snapshot_not_live_file(demo_dir):
    cfg = load_config(demo_dir)
    write_snapshot(cfg)
    text = (demo_dir / "factory.yaml").read_text(encoding="utf-8")
    (demo_dir / "factory.yaml").write_text(text.replace("^echo ", "^curl "), encoding="utf-8")
    assert "^echo " in load_for_gate(demo_dir).roles["maker"]["bash_allow"]


def test_tampered_snapshot_fails_closed(demo_dir):
    cfg = load_config(demo_dir)
    write_snapshot(cfg)
    record = json.loads(snapshot_path(demo_dir).read_text(encoding="utf-8"))
    record["text"] = record["text"].replace("^echo ", "^curl ")
    snapshot_path(demo_dir).write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ConfigError, match="sha256"):
        load_for_gate(demo_dir)


# ---------- MUST 5：舊進度轉移 ----------


def test_migrate_legacy_state(k143_dir, tmp_path):
    legacy_state = {
        "round": 10,
        "queue": ["ch04", "ch05", "ch99"],
        "chapters": {
            "ch04": {
                "status": "done",
                "rewrites": 1,
                "review_retries": 0,
                "verdicts": [],
                "calls": 7,
                "cost_usd": 1.5,
                "round": 1,
                "sessions": [],
                "started": "s",
                "finished": "f",
            },
            "ch05": {"status": "written", "calls": 3},
            "ch99": {"status": "done"},
        },
        "history": [{"ts": "t", "round": 1, "chapter": "ch04", "event": "e"}],
    }
    source = tmp_path / "old.json"
    source.write_text(json.dumps(legacy_state), encoding="utf-8")
    cfg = load_config(k143_dir)
    legacy.cmd_migrate(cfg, source, False)
    state = load_state(cfg)
    assert state["round"] == 10 and set(state["units"]) == {"ch04", "ch05"}
    ch04 = state["units"]["ch04"]
    assert ch04["status"] == "done" and ch04["calls"] == 7 and ch04["retries"] == {"rewrites": 1}
    assert state["units"]["ch05"]["status"] == "pending"
    assert state["history"][0]["unit"] == "ch04"
    with pytest.raises(RoundError, match="--force"):
        legacy.cmd_migrate(cfg, source, False)


def test_migrate_real_capstone_state(k143_dir):
    source = REPO_DIR / "capstone" / "state.json"
    if not source.exists():
        pytest.skip("capstone/state.json 不在")
    cfg = load_config(k143_dir)
    legacy.cmd_migrate(cfg, source, False)
    state = load_state(cfg)
    done = [u for u, r in state["units"].items() if r["status"] == "done"]
    assert {f"ch{n:02d}" for n in range(4, 14)} <= set(done)
    assert round_mod.pick_unit(cfg, state, None) == "ch14"


# ---------- MUST 6：工具白名單 ----------


@pytest.mark.parametrize(
    "tools, fragment",
    [
        (["Read", "write"], "未知工具"),
        (["Read", "mcp__github__create_issue"], "不支援 MCP"),
        (["Read", "Delete"], "未知工具"),
    ],
)
def test_tool_whitelist(tmp_path, tools, fragment):
    with pytest.raises(ConfigError, match=fragment):
        load_config(project_with(tmp_path, lambda d: d["roles"]["maker"].update(tools=tools)))


@pytest.mark.parametrize("tool", ["WebFetch", "MultiEdit", "NotebookEdit", "TodoWrite"])
def test_checker_only_readonly_plus_write(tmp_path, tool):
    def mutate(data):
        data["roles"]["rv"]["tools"].append(tool)
        if tool == "WebFetch":
            data["roles"]["rv"]["fetch_allow"] = ["^https://docs[.]example[.]com/"]  # 白名單合格也不行

    with pytest.raises(ConfigError, match="QA／審查角色只能用"):
        load_config(project_with(tmp_path, mutate))


# ---------- MUST 7：退回帶 raw log ----------


def test_review_reject_feedback_has_log_and_qa(demo_dir, fake_mode, monkeypatch):
    from lhf import fake

    seen = []
    original = fake.fake_agent

    def spy(cfg, rt, station, job):
        seen.append((station["id"], job["prompt_text"]))
        return original(cfg, rt, station, job)

    monkeypatch.setattr(claude_call.fake, "fake_agent", spy)
    monkeypatch.setenv("LH_FAKE_VERDICT", "FAIL_ONCE:rv")
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    round_mod.cmd_run(cfg)
    second_make = [text for sid, text in seen if sid == "make"][1]
    assert "VERDICT: REJECT" in second_make  # 審查報告
    assert "===== 檢查 =====" in second_make  # verify 的 raw log
    assert "最近一次 QA 報告" in second_make and "VERDICT: PASS" in second_make


# ---------- N1／N2／N3／N5／N6／N7／N8／N10／schema ----------


def test_atomic_replace_retries_permission_error(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("busy")
        real(src, dst)

    monkeypatch.setattr(common.os, "replace", flaky)
    common.atomic_write_text(tmp_path / "x.json", "{}")
    assert (tmp_path / "x.json").read_text(encoding="utf-8") == "{}" and calls["n"] == 3


def test_gate_budget_reads_only_after_offset(demo_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(tmp_path / "w"))
    cfg = load_config(demo_dir)
    for _ in range(5):
        append_jsonl(
            trace_path(cfg),
            {"kind": "decision", "decision": "allow", "role": "rv", "round": "1", "unit": "a", "session": "s"},
        )
    target = cfg.run_dir / "rv" / "a.md"
    event = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    ctx = {"role": "rv", "unit": "a", "round": "1", "station": "", "session": "s", "trace_offset": 0}
    assert gate.decide(event, cfg, ctx)[0] == "deny"
    ctx["trace_offset"] = trace_path(cfg).stat().st_size
    assert gate.decide(event, cfg, ctx)[0] == "allow"


def test_hours_archives_big_trace(demo_dir):
    cfg = load_config(demo_dir)
    trace_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    trace_path(cfg).write_text("x" * 10, encoding="utf-8")
    cfg.limits["trace_archive_mb"] = 0
    from lhf import hours

    archived = hours.archive_trace(cfg)
    assert archived is not None and archived.exists() and not trace_path(cfg).exists()


def test_history_is_capped(demo_dir, fake_mode):
    cfg = load_config(demo_dir)
    cfg.limits["history_max"] = 5
    round_mod.cmd_init(cfg, ["a"], False)
    round_mod.cmd_run(cfg)
    assert len(load_state(cfg)["history"]) == 5


def test_project_permissions_rejected(demo_dir, tmp_path, monkeypatch):
    work = tmp_path / "w"
    (work / ".claude").mkdir(parents=True)
    (work / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(*)"]}}), encoding="utf-8"
    )
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    cfg = load_config(demo_dir)
    with pytest.raises(RoundError, match="--allow-project-permissions"):
        claude_call.check_project_permissions(cfg, work)
    monkeypatch.setenv(claude_call.ALLOW_PROJECT_PERMISSIONS_ENV, "1")
    claude_call.check_project_permissions(cfg, work)


def test_engine_errors_threshold_marks_failed(demo_dir, tmp_path, monkeypatch):
    work = tmp_path / "w"
    work.mkdir()
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    monkeypatch.setattr(claude_call, "find_claude", lambda: [sys.executable, "-c", "import sys; sys.exit(5)"])
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    assert [round_mod.cmd_run(cfg) for _ in range(3)] == [3, 3, 3]
    assert load_state(cfg)["units"]["a"]["status"] == "failed"
    assert "連續 3 次引擎錯誤" in read_jsonl(alerts_path(cfg))[-1]["reason"]


def test_hours_stops_on_repeated_engine_errors(demo_dir, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    env = {"LH_WORK_OVERRIDE": str(work), "LH_CLAUDE_BIN": sys.executable}  # python 不認得 -p → 每次都失敗
    run_cli([str(demo_dir), "init"], env)
    proc_result = run_cli([str(demo_dir), "hours", "--hours", "1", "--gap-seconds", "0"], env)
    assert proc_result.returncode == 2, proc_result.stdout[-500:]
    assert "引擎錯誤" in (demo_dir / "run" / "ALERT.txt").read_text(encoding="utf-8")
    state = json.loads((demo_dir / "run" / "state.json").read_text(encoding="utf-8"))
    assert state["units"]["a"]["status"] == "failed" and state["units"]["c"]["status"] == "pending"


def test_command_placeholders_are_quoted(demo_dir):
    cfg = load_config(demo_dir)
    station = {"id": "x", "cmds": [{"cmd": "echo {unit} {evil}"}]}
    _, argv, _ = stations.expand_cmds(station, {"unit": "a", "evil": "a b; rm -rf /"})[0]
    assert argv == ["echo", "a", "a b; rm -rf /"]
    del cfg


def test_wsl_bash_rejected(tmp_path):
    with pytest.raises(ConfigError, match="WSL"):
        load_config(project_with(tmp_path, lambda d: d.update(bash="C:/Windows/System32/bash.exe")))


def test_verify_and_qa_max_must_match(tmp_path):
    with pytest.raises(ConfigError, match="必須相同"):
        load_config(project_with(tmp_path, lambda d: d["stations"][1].update(on_fail={"goto": "make", "max": 1})))


def test_notify_warning(tmp_path):
    project = project_with(tmp_path, lambda d: d.update(notify={"on_alert": ["line"]}))
    assert "notify 尚未實作" in run_cli([str(project), "status"]).stderr


def test_launch_failure_codes():
    assert claude_call.is_launch_failure(proc.ProcResult(127, "", "", False))
    assert claude_call.is_launch_failure(proc.ProcResult(9009, "", "", False))
    assert not claude_call.is_launch_failure(proc.ProcResult(1, "", "not found", False))


@pytest.mark.parametrize("value", [None, 2, "1"])
def test_schema_required(tmp_path, value):
    def mutate(data):
        data.pop("schema")
        if value is not None:
            data["schema"] = value

    with pytest.raises(ConfigError, match="schema"):
        load_config(project_with(tmp_path, mutate))


def test_allow_nonzero_passes(tmp_path, fake_mode):
    def mutate(data):
        data["stations"][1]["cmds"] = [
            {"title": "允許非 0", "cmd": f'"{PY}" -c "import sys; sys.exit(3)"', "allow_nonzero": True}
        ]

    cfg = load_config(project_with(tmp_path, mutate))
    round_mod.cmd_init(cfg, ["a"], False)
    assert round_mod.cmd_run(cfg) == 0


def test_nonzero_without_flag_forces_fail(tmp_path, fake_mode):
    def mutate(data):
        data["stations"][1]["cmds"] = [
            {"title": "第一個", "cmd": f'"{PY}" -c "import sys; sys.exit(2)"'},
            {"title": "第二個", "cmd": f'"{PY}" -c "print(1)"'},
        ]

    cfg = load_config(project_with(tmp_path, mutate))
    round_mod.cmd_init(cfg, ["a"], False)
    assert round_mod.cmd_run(cfg) == 1
    raw = (cfg.run_dir / "raw" / "a.log").read_text(encoding="utf-8")
    assert "===== 第二個 =====" in raw  # 沒有 fail_on_error：後面的指令照跑，log 完整


def test_finish_command_failure_marks_failed(tmp_path, fake_mode):
    def mutate(data):
        data["stations"].append(
            {
                "id": "deploy",
                "kind": "command",
                "chain": "finish",
                "log": "{RUN}/deploy.log",
                "cmds": [{"cmd": f'"{PY}" -c "import sys; sys.exit(1)"'}],
                "unattended": True,  # 測試不經 approve 直接跑收尾指令
            }
        )

    cfg = load_config(project_with(tmp_path, mutate))
    round_mod.cmd_init(cfg, ["a"], False)
    assert round_mod.cmd_run(cfg) == 1
    assert load_state(cfg)["units"]["a"]["status"] == "failed"
    assert "收尾站" in read_jsonl(alerts_path(cfg))[-1]["reason"]


def test_finish_command_cannot_have_on_fail(tmp_path):
    def mutate(data):
        data["stations"].append(
            {
                "id": "deploy",
                "kind": "command",
                "chain": "finish",
                "log": "{RUN}/d.log",
                "cmds": [{"cmd": "echo"}],
                "on_fail": {"goto": "make"},
            }
        )

    with pytest.raises(ConfigError, match="finish 鏈的 command 站不可有 on_fail"):
        load_config(project_with(tmp_path, mutate))


def test_common_constants_single_source():
    from lhf import hours

    assert round_mod.USD_TO_TWD is common.USD_TO_TWD and hours.USD_TO_TWD is common.USD_TO_TWD
    assert round_mod.EXIT_INTERRUPTED == hours.EXIT_INTERRUPTED == 130
    assert Path(gate.__file__).name == "gate.py" and gate.now_iso is common.now_iso
