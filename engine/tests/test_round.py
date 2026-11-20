"""假模型模式的整輪流程：基準站序、退回、上限、中斷接續、核准、依賴挑選、預算、reset。"""

import copy
import re
import shutil

import pytest

from conftest import PY, demo_config, write_project
from lhf import round as round_mod
from lhf.config import load_config
from lhf.state import alerts_path, approved_path, load_state, pending_approvals_path, read_jsonl, trace_path
from lhf.stations import read_verdict

pytestmark = pytest.mark.usefixtures("fake_mode")
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash 跑 k143 的 command 站")
STATION_RE = re.compile(r"^\[站 (.+?)\]", re.MULTILINE)


def run_once(cfg, capsys, unit=None):
    code = round_mod.cmd_run(cfg, unit)
    out = capsys.readouterr().out
    return code, STATION_RE.findall(out), out


def k143_ids(labels):
    mapping = {
        "1 規劃": "plan",
        "2 寫腳本＋現況（平行）": "write",
        "3 執行腳本": "run",
        "4 測試判定": "judge",
        "5 審查": "review",
        "6 commit": "commit",
    }
    return [mapping[label] for label in labels]


def unit_of(cfg, unit):
    return load_state(cfg)["units"][unit]


@needs_bash
def test_k143_fail_once_matches_legacy_baseline(k143_dir, monkeypatch, capsys):
    """舊版基準：plan→write→run→judge FAIL→write(writer)→run→judge PASS→review PASS→commit 略過，calls=7。"""
    monkeypatch.setenv("LH_FAKE_VERDICT", "FAIL_ONCE")
    cfg = load_config(k143_dir)
    round_mod.cmd_init(cfg, ["ch04"], False)
    code, labels, out = run_once(cfg, capsys)
    assert code == 0
    assert k143_ids(labels) == ["plan", "write", "run", "judge", "write", "run", "judge", "review", "commit"]
    rec = unit_of(cfg, "ch04")
    assert rec["status"] == "done" and rec["calls"] == 7 and rec["retries"] == {"rewrites": 1}
    assert [(v["station"], v["verdict"]) for v in rec["verdicts"]] == [
        ("judge", "FAIL"),
        ("judge", "PASS"),
        ("review", "PASS"),
    ]
    assert rec["once_done"] == ["now-updater"] and len(rec["sessions"]) == 7
    assert "2 寫腳本＋現況（平行） 完成（writer）" in out  # 重寫輪不再重做現況
    raw = (cfg.run_dir / "test-logs" / "ch04-raw.log").read_text(encoding="utf-8")
    assert raw.count("=====") == 18 and "exit code：0" in raw


@needs_bash
def test_k143_review_reject_reruns_verify_and_qa(k143_dir, monkeypatch, capsys):
    monkeypatch.setenv("LH_FAKE_VERDICT", "FAIL_ONCE:review")
    cfg = load_config(k143_dir)
    round_mod.cmd_init(cfg, ["ch05"], False)
    code, labels, _ = run_once(cfg, capsys)
    assert code == 0
    assert k143_ids(labels) == ["plan", "write", "run", "judge", "review", "write", "run", "judge", "review", "commit"]
    rec = unit_of(cfg, "ch05")
    assert rec["retries"] == {"review_retries": 1} and rec["calls"] == 8
    feedback_hint = [r for r in read_jsonl(trace_path(cfg)) if r.get("kind") == "rework"]
    assert feedback_hint and feedback_hint[0]["decision"] == "write"


def test_qa_fail_always_marks_failed_and_alerts(demo_dir, monkeypatch, capsys):
    monkeypatch.setenv("LH_FAKE_VERDICT", "FAIL_ALWAYS:qa")
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    code, labels, _ = run_once(cfg, capsys)
    assert code == 1
    assert labels == ["make", "check", "qa"] * 3
    rec = unit_of(cfg, "a")
    assert rec["status"] == "failed" and rec["retries"] == {"rewrites": 2}
    alerts = read_jsonl(alerts_path(cfg))
    assert alerts and alerts[-1]["unit"] == "a" and alerts[-1]["status"] == "failed"
    code, labels, out = run_once(cfg, capsys)
    assert labels == [] and "沒有可執行" in out  # 下一輪不自動重做


def test_review_fail_always_marks_rejected(demo_dir, monkeypatch, capsys):
    monkeypatch.setenv("LH_FAKE_VERDICT", "FAIL_ALWAYS:rv")
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    code, labels, _ = run_once(cfg, capsys)
    assert code == 1
    assert labels == ["make", "check", "qa", "rv", "make", "check", "qa", "rv"]
    rec = unit_of(cfg, "a")
    assert rec["status"] == "rejected" and rec["retries"] == {"review_retries": 1}
    assert read_jsonl(alerts_path(cfg))[-1]["status"] == "rejected"


def test_verify_auto_fail_skips_qa_call(tmp_path, monkeypatch, capsys):
    data = copy.deepcopy(demo_config())
    data["stations"][1]["cmds"] = [
        {"title": "必失敗", "cmd": f'"{PY}" -c "import sys; sys.exit(3)"', "fail_on_error": True}
    ]
    cfg = load_config(write_project(tmp_path, data))
    round_mod.cmd_init(cfg, ["a"], False)
    code, labels, _ = run_once(cfg, capsys)
    assert code == 1 and labels == ["make", "check"] * 3
    rec = unit_of(cfg, "a")
    assert rec["status"] == "failed" and rec["calls"] == 3
    report = (cfg.run_dir / "qa" / "a.md").read_text(encoding="utf-8")
    assert "VERDICT: FAIL" in report and "必失敗失敗" in report


def test_feedback_reaches_producer_prompt(demo_dir, monkeypatch, capsys):
    """退回時 producer 的 prompt 要帶上 QA 報告內容。"""
    from lhf import claude_call, fake

    seen = []
    original = fake.fake_agent

    def spy(cfg, rt, station, job):
        seen.append((station["id"], job["prompt_text"]))
        return original(cfg, rt, station, job)

    monkeypatch.setattr(claude_call.fake, "fake_agent", spy)
    monkeypatch.setenv("LH_FAKE_VERDICT", "FAIL_ONCE:qa")
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    run_once(cfg, capsys)
    make_prompts = [text for sid, text in seen if sid == "make"]
    assert len(make_prompts) == 2 and "VERDICT: FAIL" in make_prompts[1] and "VERDICT" not in make_prompts[0]


@needs_bash
def test_interrupt_and_resume_does_not_redo(k143_dir, monkeypatch, capsys):
    monkeypatch.setenv("LH_FAKE_INTERRUPT_AFTER", "write")
    cfg = load_config(k143_dir)
    round_mod.cmd_init(cfg, ["ch04"], False)
    code, labels, _ = run_once(cfg, capsys)
    assert code == 130 and k143_ids(labels) == ["plan", "write"]
    rec = unit_of(cfg, "ch04")
    assert rec["station"] == "run" and rec["calls"] == 3
    monkeypatch.delenv("LH_FAKE_INTERRUPT_AFTER")
    code, labels, out = run_once(cfg, capsys)
    assert code == 0 and k143_ids(labels) == ["run", "judge", "review", "commit"]
    assert "接續第 1 輪" in out
    rec = unit_of(cfg, "ch04")
    assert rec["status"] == "done" and rec["calls"] == 5 and rec["round"] == 1


def approve_project(tmp_path):
    data = copy.deepcopy(demo_config())
    data["stations"].insert(4, {"id": "publish", "kind": "approve", "chain": "finish", "message": "發布 {unit}？"})
    return load_config(write_project(tmp_path, data))


def test_approve_station_flow(tmp_path, capsys):
    cfg = approve_project(tmp_path)
    round_mod.cmd_init(cfg, ["a"], False)
    code, labels, _ = run_once(cfg, capsys)
    assert code == 0 and labels[-1] == "publish"
    rec = unit_of(cfg, "a")
    assert rec["status"] == "pending_approval" and rec["station"] == "publish"
    assert read_jsonl(pending_approvals_path(cfg))[0]["command"] == "發布 a？"
    code, labels, out = run_once(cfg, capsys)
    assert labels == [] and "沒有可執行" in out
    round_mod.cmd_approve(cfg, "a")
    assert read_jsonl(pending_approvals_path(cfg)) == [] and len(read_jsonl(approved_path(cfg))) == 1
    code, labels, _ = run_once(cfg, capsys)
    assert code == 0 and labels == ["publish", "done"]
    rec = unit_of(cfg, "a")
    assert rec["status"] == "done" and rec["approved"] == []


def test_approve_requires_pending(demo_dir):
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    with pytest.raises(round_mod.RoundError, match="沒有在等待核准"):
        round_mod.cmd_approve(cfg, "a")


def test_gate_approval_request_pauses_station(demo_dir, monkeypatch, capsys):
    monkeypatch.setenv("LH_FAKE_APPROVAL_REQUEST", "maker")
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    code, labels, _ = run_once(cfg, capsys)
    assert code == 0 and labels == ["make"]
    assert unit_of(cfg, "a")["status"] == "pending_approval"
    round_mod.cmd_approve(cfg, "a")
    monkeypatch.delenv("LH_FAKE_APPROVAL_REQUEST")
    code, labels, _ = run_once(cfg, capsys)
    assert code == 0 and labels[0] == "make" and unit_of(cfg, "a")["status"] == "done"


def test_depends_on_topological_pick(tmp_path, capsys):
    data = copy.deepcopy(demo_config())
    data["units"]["depends_on"] = {"a": ["b"], "b": ["c"]}
    cfg = load_config(write_project(tmp_path, data))
    round_mod.cmd_init(cfg, ["a", "b", "c"], False)
    order = []
    for _ in range(3):
        before = {u for u, r in load_state(cfg)["units"].items() if r["status"] == "done"}
        assert run_once(cfg, capsys)[0] == 0
        order += [u for u, r in load_state(cfg)["units"].items() if r["status"] == "done" and u not in before]
    assert order == ["c", "b", "a"]


def test_blocked_dependency_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LH_FAKE_VERDICT", "FAIL_ALWAYS:qa")
    data = copy.deepcopy(demo_config())
    data["units"]["depends_on"] = {"a": ["b"]}
    cfg = load_config(write_project(tmp_path, data))
    round_mod.cmd_init(cfg, ["a", "b"], False)
    run_once(cfg, capsys)  # b 先跑並失敗
    code, labels, out = run_once(cfg, capsys, unit="a")
    assert labels == [] and "依賴的 b 尚未完成" in out


def test_call_budget_exceeded_marks_failed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LH_FAKE_VERDICT", "FAIL_ALWAYS:qa")
    data = copy.deepcopy(demo_config())
    data["limits"] = {"calls_per_unit": 3}
    cfg = load_config(write_project(tmp_path, data))
    round_mod.cmd_init(cfg, ["a"], False)
    code, _, _ = run_once(cfg, capsys)
    rec = unit_of(cfg, "a")
    assert code == 1 and rec["status"] == "failed" and rec["calls"] == 3
    assert "超過上限" in read_jsonl(alerts_path(cfg))[-1]["reason"]


def test_reset_and_reset_calls(demo_dir, monkeypatch, capsys):
    monkeypatch.setenv("LH_FAKE_VERDICT", "FAIL_ALWAYS:qa")
    cfg = load_config(demo_dir)
    round_mod.cmd_init(cfg, ["a"], False)
    run_once(cfg, capsys)
    round_mod.cmd_reset_calls(cfg, "a")
    assert unit_of(cfg, "a")["calls"] == 0 and unit_of(cfg, "a")["status"] == "failed"
    round_mod.cmd_reset(cfg, "a")
    assert unit_of(cfg, "a")["status"] == "pending"
    monkeypatch.delenv("LH_FAKE_VERDICT")
    assert run_once(cfg, capsys)[0] == 0 and unit_of(cfg, "a")["status"] == "done"


def test_init_rejects_unknown_unit(demo_dir):
    with pytest.raises(round_mod.RoundError, match="units.ids"):
        round_mod.cmd_init(load_config(demo_dir), ["zzz"], False)


def test_run_without_state(demo_dir):
    with pytest.raises(round_mod.RoundError, match="init"):
        round_mod.cmd_run(load_config(demo_dir))


def test_status_and_dry_run_smoke(k143_dir, capsys):
    cfg = load_config(k143_dir)
    assert round_mod.cmd_dry_run(cfg, "ch07") == 0
    out = capsys.readouterr().out
    assert "--tools Read Glob Grep Write" in out and "bash ch07.sh 8" in out
    round_mod.cmd_init(cfg, None, False)
    assert round_mod.cmd_status(cfg) == 0
    assert "ch18" in capsys.readouterr().out


@pytest.mark.parametrize(
    "content, expected",
    [
        (None, "FAIL"),
        ("", "FAIL"),
        ("報告\n\nVERDICT: PASS\n", "PASS"),
        ("VERDICT: PASS\n多一行\n", "FAIL"),
        ("`VERDICT: PASS`", "PASS"),
        ("VERDICT: pass", "FAIL"),
        ("VERDICT: OK", "FAIL"),
    ],
)
def test_read_verdict_fail_closed(tmp_path, content, expected):
    path = tmp_path / "report.md"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert read_verdict(path, ("PASS", "FAIL"))[0] == expected
