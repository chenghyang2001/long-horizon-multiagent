"""hours 連續執行、CLI 入口、與舊版 prompt 等價、引擎不含專案專屬字。"""

import copy
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ENGINE_DIR, REPO_DIR, demo_config, write_project
from lhf import stations
from lhf.config import load_config


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


def test_hours_runs_until_queue_empty(demo_dir):
    fake = {"LH_FAKE_MODEL": "1"}
    assert run_cli([str(demo_dir), "init"], fake).returncode == 0
    proc = run_cli([str(demo_dir), "hours", "--hours", "1", "--max-rounds", "5", "--gap-seconds", "0"], fake)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    summaries = list((demo_dir / "run" / "logs").glob("hours-summary-*.md"))
    text = summaries[0].read_text(encoding="utf-8")
    assert "沒有可執行的工作單位" in text and "完成單位：3/3" in text and "## 退回紀錄" in text


def test_hours_alert_after_two_failures(demo_dir):
    env = {"LH_FAKE_MODEL": "1", "LH_FAKE_VERDICT": "FAIL_ALWAYS:qa"}
    run_cli([str(demo_dir), "init"], env)
    proc = run_cli([str(demo_dir), "hours", "--hours", "1", "--gap-seconds", "0"], env)
    assert proc.returncode == 1
    assert (demo_dir / "run" / "ALERT.txt").exists()
    summary = next((demo_dir / "run" / "logs").glob("hours-summary-*.md")).read_text(encoding="utf-8")
    assert "qa FAIL" in summary and "ALERT 清單（本次單位）：2 筆" in summary


def test_cli_config_error_exit_2(tmp_path):
    data = copy.deepcopy(demo_config())
    data["stations"].pop(3)
    project = write_project(tmp_path, data)
    proc = run_cli([str(project), "status"])
    assert proc.returncode == 2 and "拒絕啟動" in proc.stderr and "review" in proc.stderr


def test_cli_rejects_bad_unit_id(demo_dir):
    assert run_cli([str(demo_dir), "run", "--unit", "../x"]).returncode == 2


def test_cli_hours_validation(demo_dir):
    assert run_cli([str(demo_dir), "hours", "--hours", "0"]).returncode == 2


# ---------- 與舊版 capstone/run_round.py 的 prompt 逐字等價 ----------

LEGACY = REPO_DIR / "capstone" / "run_round.py"


def load_legacy():
    spec = importlib.util.spec_from_file_location("legacy_run_round", LEGACY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not LEGACY.exists(), reason="舊版 capstone 不在")
@pytest.mark.parametrize("unit", ["ch04", "ch11", "ch18"])
def test_k143_prompts_match_legacy(k143_dir, unit, tmp_path):
    legacy = load_legacy()
    cfg = load_config(k143_dir)
    overrides = {"WORK": legacy.WORKTREE.as_posix(), "RUN": legacy.CAPSTONE.as_posix()}
    cfg.vars = lambda unit=None, round_no=None, overrides_=overrides, original=cfg.vars: original(
        unit=unit, round_no=round_no, overrides=overrides_
    )
    feedback = tmp_path / "fb.md"
    feedback.write_text("第 3 步壞了 {大括號}\nVERDICT: FAIL\n", encoding="utf-8")
    rec = {"feedback": None}
    rt = {"cfg": cfg, "state": {"units": {unit: rec}}, "unit": unit, "round": 1}
    paths = legacy.chapter_paths(unit)
    assert stations.prompt_text(rt, "plan") == legacy.prompt_plan(unit, paths)
    assert stations.prompt_text(rt, "write") == legacy.prompt_write(unit, paths, {"feedback": None})
    assert stations.prompt_text(rt, "now") == legacy.prompt_now(unit, paths)
    assert stations.prompt_text(rt, "judge") == legacy.prompt_judge(unit, paths)
    assert stations.prompt_text(rt, "review") == legacy.prompt_review(unit, paths)
    rec["feedback"] = {"label": "測試", "path": str(feedback), "for": "write"}
    assert stations.prompt_text(rt, "write") == legacy.prompt_write(
        unit, paths, {"feedback": {"kind": "test", "path": str(feedback)}}
    )


def test_engine_has_no_project_specific_words():
    banned = re.compile(r"K143|fable|Fable|教練|\bch\d{2}\b|chapter|章|8 步", re.IGNORECASE)
    hits = []
    for path in (ENGINE_DIR / "lhf").glob("*.py"):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if banned.search(line):
                hits.append(f"{path.name}:{no}: {line.strip()}")
    assert not hits, "\n".join(hits)


def test_template_loads(tmp_path):
    import shutil

    target = tmp_path / "tpl"
    shutil.copytree(REPO_DIR / "templates" / "kindle-drill", target)
    cfg = load_config(target)
    assert [s["chain"] for s in cfg.stations][-3:] == ["qa", "review", "finish"]
    assert Path(cfg.agents_dir).is_dir()
