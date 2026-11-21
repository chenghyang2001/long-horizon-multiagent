"""B 階段 code-reviewer 退回項目：commit 站 hook 與環境、控制檔鎖存＋持久基準＋ack-control、stop 預設不強制、
verify-commits、pending-info --diff 與不可信內容跳脫、只對本次新增失敗發 ALERT、launch-hours.sh。

不打真的 Telegram、不呼叫真的 claude 模型。
"""

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ENGINE_DIR, PY, demo_config, write_project

from lhf import gate, hours, integrity, lock, pending, stations, stop
from lhf import round as round_mod
from lhf.common import EXIT_CONTROL_CHANGED, IN_ENGINE_ENV, scrubbed_env
from lhf.config import load_config
from lhf.errors import RoundError
from lhf.state import load_state

HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="需要 git")
SECRET = "123456:SECRET-token-should-never-leak"


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


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL
    ).stdout


def git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "user.name", "t")
    (path / "README.md").write_text("x", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-qm", "init")
    return path


def project_at(tmp_path, work, mutate=None, name="demo"):
    data = copy.deepcopy(demo_config())
    data["workdir"] = work.as_posix()
    if mutate:
        mutate(data)
    return write_project(tmp_path, data, name)


# ---------- MUST 1：commit 站 hook 與環境 ----------


def husky_repo(tmp_path):
    work = git_repo(tmp_path / "work")
    dump = tmp_path / "hook-env.txt"
    (work / ".husky").mkdir()
    (work / ".husky" / "pre-commit").write_text(f'#!/bin/sh\nenv > "{dump.as_posix()}"\n', encoding="utf-8")
    git(work, "config", "core.hooksPath", ".husky")
    (work / "a.txt").write_text("產出", encoding="utf-8")
    return work, dump


def commit_rt(cfg):
    round_mod.cmd_init(cfg, ["a"], False)
    state = load_state(cfg)
    state["units"]["a"].update(station="done", status="in_progress", round=1)
    return {"cfg": cfg, "state": state, "unit": "a", "round": 1}


@needs_git
def test_commit_station_skips_hooks_by_default(tmp_path, monkeypatch):
    work, dump = husky_repo(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    monkeypatch.setenv("LH_LOCK_TOKEN", "lock-secret")
    cfg = load_config(project_at(tmp_path, work))
    rt = commit_rt(cfg)
    assert stations.station_commit(rt, cfg.station("done")) == "已 commit"
    assert not dump.exists()  # reviewer 的 husky 重現：預設不跑 hook
    assert rt["state"]["units"]["a"]["commits"] == [git(work, "rev-parse", "HEAD").strip()]


@needs_git
def test_commit_station_hooks_true_gets_scrubbed_env(tmp_path, monkeypatch):
    work, dump = husky_repo(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987")
    monkeypatch.setenv("LH_LOCK_TOKEN", "lock-secret")
    cfg = load_config(project_at(tmp_path, work, lambda d: d["stations"][4].update(hooks=True)))
    rt = commit_rt(cfg)
    stations.station_commit(rt, cfg.station("done"))
    env_text = dump.read_text(encoding="utf-8", errors="replace")
    names = {line.split("=", 1)[0].upper() for line in env_text.splitlines() if "=" in line}
    assert not [n for n in names if n.startswith(("TELEGRAM_", "LH_"))]
    assert SECRET not in env_text and "lock-secret" not in env_text
    assert f"{IN_ENGINE_ENV}=1" in env_text  # hook 裡若想執行 lhf approve 會被第一層擋下


@needs_git
def test_hooks_true_puts_hook_files_into_control_snapshot(tmp_path):
    work, _ = husky_repo(tmp_path)
    (work / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    off = load_config(project_at(tmp_path, work, name="off"))
    on = load_config(project_at(tmp_path, work, lambda d: d["stations"][4].update(hooks=True), name="on"))
    hook = str(work / ".husky" / "pre-commit").lower()  # 控制檔路徑以小寫正規化
    assert hook not in integrity.control_snapshot(off)
    base = integrity.control_snapshot(on)
    assert hook in base and str(work / ".pre-commit-config.yaml").lower() in base
    (work / ".husky" / "pre-commit").write_text("#!/bin/sh\necho pwn\n", encoding="utf-8")
    assert hook in integrity.diff(base, integrity.control_snapshot(on))


def test_commit_hooks_must_be_bool(tmp_path):
    from lhf.errors import ConfigError

    data = copy.deepcopy(demo_config())
    data["stations"][4]["hooks"] = "yes"
    with pytest.raises(ConfigError, match="hooks"):
        load_config(write_project(tmp_path, data))


def test_scrubbed_env_has_no_engine_or_notify_vars(monkeypatch):
    for key, value in (("TELEGRAM_BOT_TOKEN", SECRET), ("TELEGRAM_CHAT_ID_X", "1"), ("LH_LOCK_TOKEN", "t")):
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LHF_SOMETHING", "x")
    env = scrubbed_env()
    leaked = [k for k in env if k.upper().startswith(("TELEGRAM_", "LH_", "LHF_")) and k != IN_ENGINE_ENV]
    assert not leaked and env[IN_ENGINE_ENV] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert IN_ENGINE_ENV not in scrubbed_env(in_engine=False)


# ---------- MUST 2：鎖存＋持久基準＋ack-control ----------


def planted_project(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / ".git" / "hooks").mkdir(parents=True)
    (work / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    script = tmp_path / "plant.py"
    hook = (work / ".git" / "hooks" / "pre-commit").as_posix()
    script.write_text(f"open(r'{hook}','w').write('echo pwn')\n", encoding="utf-8")
    data = copy.deepcopy(demo_config())
    data["stations"][1]["cmds"] = [{"title": "植入 hook", "cmd": f'"{PY}" "{script.as_posix()}"'}]
    return write_project(tmp_path, data), work


def test_latch_blocks_restart_until_ack(tmp_path, monkeypatch):
    project, work = planted_project(tmp_path, monkeypatch)
    env = {"LH_FAKE_MODEL": "1", "LH_WORK_OVERRIDE": str(work)}
    run_cli([str(project), "init"], env)
    first = run_cli([str(project), "hours", "--hours", "1", "--gap-seconds", "0"], env)
    assert first.returncode == EXIT_CONTROL_CHANGED, first.stdout[-600:]
    latch = project / "run" / "CONTROL-CHANGED.json"
    assert latch.exists() and "pre-commit" in latch.read_text(encoding="utf-8")
    for command in (["run"], ["hours", "--hours", "1", "--gap-seconds", "0"]):
        again = run_cli([str(project), *command], env)
        assert again.returncode == EXIT_CONTROL_CHANGED and "ack-control" in again.stderr  # 重開不會把 hook 當新基準
    status = run_cli([str(project), "status"], env)
    assert "CONTROL-CHANGED" in status.stdout and "結束碼 6" in status.stdout
    show = run_cli([str(project), "ack-control"], env)
    assert show.returncode == 0 and "pre-commit" in show.stdout and latch.exists()  # 沒加 --yes 只顯示
    done = run_cli([str(project), "ack-control", "--yes"], env)
    assert done.returncode == 0 and not latch.exists(), done.stdout + done.stderr
    base = json.loads((project / "run" / "control-baseline.json").read_text(encoding="utf-8"))
    assert str(work / ".git" / "hooks" / "pre-commit").lower() in base["entries"]


def test_deleting_latch_without_ack_still_detected(tmp_path, monkeypatch, fake_mode):
    """有人直接刪掉鎖存檔：持久基準沒變，下一次 run 照樣比對出差異並重新鎖存。"""
    work = tmp_path / "work"
    (work / ".git" / "hooks").mkdir(parents=True)
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    cfg = load_config(write_project(tmp_path, demo_config()))
    round_mod.cmd_init(cfg, ["a"], False)
    assert integrity.check_baseline(cfg, "test") == ""  # 第一次：建立基準
    (work / ".git" / "hooks" / "pre-push").write_text("evil", encoding="utf-8")
    assert integrity.check_baseline(cfg, "test") and integrity.latch_path(cfg).exists()
    integrity.latch_path(cfg).unlink()
    assert round_mod.cmd_run(cfg) == EXIT_CONTROL_CHANGED and integrity.latch_path(cfg).exists()
    assert load_state(cfg)["units"]["a"]["status"] == "pending"


def test_corrupt_baseline_latches(demo_dir, fake_mode):
    cfg = load_config(demo_dir)
    integrity.baseline_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    integrity.baseline_path(cfg).write_text("{壞", encoding="utf-8")
    assert "讀不懂" in integrity.check_baseline(cfg, "test") and integrity.read_latch(cfg)


def test_ack_control_refused_from_engine_descendant(demo_dir):
    assert run_cli([str(demo_dir), "ack-control", "--yes"], {IN_ENGINE_ENV: "1"}).returncode == 5


def test_ack_control_refused_while_engine_running(demo_dir):
    cfg = load_config(demo_dir)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        lock.lock_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        lock.lock_path(cfg).write_text(
            json.dumps({"pid": holder.pid, "started": lock.process_start_time(holder.pid), "token": "t"}),
            encoding="utf-8",
        )
        assert run_cli([str(demo_dir), "ack-control", "--yes"]).returncode == 4
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.parametrize("name", ["CONTROL-CHANGED.json", "control-baseline.json"])
def test_gate_protects_latch_and_baseline(demo_dir, name):
    cfg = load_config(demo_dir)
    target = (cfg.run_dir / name).as_posix()
    event = {"tool_name": "Write", "tool_input": {"file_path": target}}
    ctx = {"role": "maker", "unit": "a", "round": "1", "station": "", "session": "s", "trace_offset": 0}
    assert gate.decide(event, cfg, ctx)[0] == "deny"


def test_latch_file_is_restored_if_command_station_deletes_it(tmp_path, monkeypatch, fake_mode):
    cfg = load_config(write_project(tmp_path, demo_config()))
    integrity.write_latch(cfg, "test", "x", [])
    before = integrity.snapshot(cfg)
    integrity.latch_path(cfg).unlink()
    changed = integrity.diff(before, integrity.snapshot(cfg))
    assert str(integrity.latch_path(cfg)) in changed
    integrity.restore(before, changed)
    assert integrity.latch_path(cfg).exists()


# ---------- MUST 3：stop 預設不強制 ----------


@pytest.fixture
def holder(demo_dir):
    cfg = load_config(demo_dir)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    lock.lock_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    lock.lock_path(cfg).write_text(
        json.dumps({"pid": child.pid, "started": lock.process_start_time(child.pid), "token": "t"}), encoding="utf-8"
    )
    yield cfg, child
    child.kill()
    child.wait()


def test_stop_without_force_never_kills(holder, capsys):
    cfg, child = holder
    assert stop.cmd_stop(cfg, wait=1) == 4
    assert child.poll() is None and stop.requested(cfg)  # 程序還活著、停止要求仍有效
    assert "--force" in capsys.readouterr().out


def test_cli_stop_force_flags(holder, demo_dir):
    _cfg, child = holder
    assert run_cli([str(demo_dir), "stop", "--wait", "1", "--no-force"]).returncode == 4 and child.poll() is None
    assert run_cli([str(demo_dir), "stop", "--wait", "1", "--force", "--no-force"]).returncode == 2  # 互斥
    assert run_cli([str(demo_dir), "stop", "--wait", "1", "--force"]).returncode == 1
    assert child.wait(timeout=30) is not None


# ---------- MUST 5：verify-commits ----------


@needs_git
def test_verify_commits(tmp_path, fake_mode, monkeypatch, capsys):
    work = git_repo(tmp_path / "work")
    head = git(work, "rev-parse", "HEAD").strip()
    git(work, "checkout", "-qb", "other")
    (work / "o.txt").write_text("o", encoding="utf-8")
    git(work, "add", "o.txt")
    git(work, "commit", "-qm", "other")
    other = git(work, "rev-parse", "HEAD").strip()
    git(work, "checkout", "-q", "main")
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    cfg = load_config(write_project(tmp_path, demo_config()))
    round_mod.cmd_init(cfg, ["a", "b"], False)
    path = cfg.run_dir / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["units"]["a"]["commits"] = [head]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert pending.cmd_verify_commits(cfg) == 0
    data["units"]["b"]["commits"] = [other, "0" * 40]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert pending.cmd_verify_commits(cfg) == 1
    out = capsys.readouterr().out
    assert "不在目前分支上" in out and "不存在" in out and "缺 2 個" in out


# ---------- MUST 4＋一併修：pending-info 的不可信內容、--diff、欄位名 ----------


@needs_git
def test_pending_info_diff_is_escaped_and_safe(tmp_path, monkeypatch, capsys, fake_mode):
    work = git_repo(tmp_path / "work")
    evil = "\x1b[2J清畫面\u202e反向\x07"
    (work / "README.md").write_text(f"改了 {evil}\n", encoding="utf-8")
    (work / "new.sh").write_text("echo 新檔\x1b[31m\n", encoding="utf-8")
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    cfg = load_config(write_project(tmp_path, demo_config()))
    round_mod.cmd_init(cfg, ["a"], False)
    assert pending.cmd_pending_info(cfg, "a", "README.md") == 0
    assert pending.cmd_pending_info(cfg, "a", "new.sh") == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\u202e" not in out and "\x07" not in out
    assert "\\x1b" in out and "\\u202e" in out and "未追蹤的新檔" in out
    for bad in ("../x", "/etc/passwd", "C:/Windows/win.ini", "a/../../x"):
        with pytest.raises(RoundError, match="相對路徑|跑出工作目錄"):
            pending.cmd_pending_info(cfg, "a", bad)


@needs_git
def test_pending_info_json_fields_and_sanitized(tmp_path, monkeypatch, capsys, fake_mode):
    work = git_repo(tmp_path / "work")
    (work / "build.sh").write_text("x", encoding="utf-8")
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    data = copy.deepcopy(demo_config())
    data["stations"].insert(4, {"id": "pub", "kind": "approve", "chain": "finish", "message": "發布\u202e{unit}"})
    cfg = load_config(write_project(tmp_path, data))
    round_mod.cmd_init(cfg, ["a"], False)
    round_mod.cmd_run(cfg)
    capsys.readouterr()
    assert pending.cmd_pending_info(cfg, "a") == 0
    raw = capsys.readouterr().out
    info = json.loads(raw)
    assert "\u202e" not in raw and "\\u202e" in info["requests"][0]["command"]
    assert info["workdir_changes"] == ["a.txt", "build.sh"] and "不一定都是本單位" in info["workdir_changes_note"]
    assert info["unit_commit_files"] == []
    assert info["executable_files"] == [{"path": "build.sh", "reason": "shell 腳本", "source": "workdir"}]


@needs_git
def test_pending_status_ignores_submodules(tmp_path, monkeypatch):
    work = git_repo(tmp_path / "work")
    monkeypatch.setenv("LH_WORK_OVERRIDE", str(work))
    calls = []
    original = pending.raw_git

    def spy(work_dir, args, prefix=pending.SAFE_GIT_ARGS):
        calls.append(args)
        return original(work_dir, args, prefix)

    monkeypatch.setattr(pending, "raw_git", spy)
    pending.status_paths(work)
    assert any("--ignore-submodules=all" in args for args in calls)


# ---------- 一併修：只對本次新增的失敗發 ALERT ----------


def test_old_failures_not_realerted(demo_dir, fake_mode, monkeypatch):
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    path = cfg.run_dir / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["units"]["a"]["status"] = "failed"
    path.write_text(json.dumps(data), encoding="utf-8")
    sent = []
    monkeypatch.setattr(hours.notify, "send", lambda *a, **k: sent.append(k) or False)
    log = cfg.run_dir / "h.log"
    with log.open("w", encoding="utf-8") as handle:
        ctx = {"cfg": cfg, "log": handle, "log_path": log, "alert_lines": [], "failed_at_start": {"a"}}
        assert hours.finalize_alerts(ctx, 0) == 0
        assert not (cfg.run_dir / "ALERT.txt").exists() and not sent
        ctx["failed_at_start"] = set()
        assert hours.finalize_alerts(ctx, 0) == 1 and (cfg.run_dir / "ALERT.txt").exists()


# ---------- 一併修：launch-hours.sh ----------


def git_bash():
    found = shutil.which("bash")
    if not found or "system32" in found.lower() or "windowsapps" in found.lower():
        return None
    return found


needs_bash = pytest.mark.skipif(git_bash() is None, reason="需要 Git Bash")
LAUNCHER = ENGINE_DIR / "launch-hours.sh"


def run_launcher(*args, env_extra=None):
    env = {**os.environ, "PYTHONUTF8": "1", **(env_extra or {})}
    return subprocess.run(
        [git_bash(), str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=300,
        check=False,
    )


@needs_bash
@pytest.mark.parametrize("args", [[], ["only-one"], ["{p}", "abc"], ["{p}", "0"], ["{p}", "-1"], ["/nope", "1"]])
def test_launch_hours_rejects_bad_args(demo_dir, args):
    result = run_launcher(*[a.format(p=demo_dir.as_posix()) for a in args])
    assert result.returncode == 2 and ("錯誤" in result.stderr or "用法" in result.stderr)


@needs_bash
def test_launch_hours_runs_and_prints_exit_code(demo_dir):
    env = {"LH_FAKE_MODEL": "1"}
    run_cli([str(demo_dir), "init"], env)
    result = run_launcher(demo_dir.as_posix(), "1", "--max-rounds", "1", "--gap-seconds", "0", env_extra=env)
    assert "hours 結束碼 0" in result.stdout, result.stdout[-800:] + result.stderr[-400:]
    assert load_state(load_config(demo_dir))["units"]["a"]["status"] == "done"


def test_launch_hours_is_shell_syntax_valid():
    bash = git_bash()
    if bash is None:
        pytest.skip("需要 Git Bash")
    checked = subprocess.run([bash, "-n", str(LAUNCHER)], stdin=subprocess.DEVNULL, capture_output=True, check=False)
    assert checked.returncode == 0
    assert Path(LAUNCHER).read_text(encoding="utf-8").startswith("#!/bin/bash\nset -euo pipefail\n")
