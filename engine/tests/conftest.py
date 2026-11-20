"""測試共用：把 engine/ 加進 sys.path、清掉會影響結果的環境變數、提供專案範本。"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ENGINE_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = ENGINE_DIR.parent
K143_DIR = REPO_DIR / "projects" / "k143"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

PY = Path(sys.executable).as_posix()
LH_VARS = (
    "LH_FAKE_MODEL",
    "LH_FAKE_VERDICT",
    "LH_FAKE_INTERRUPT_AFTER",
    "LH_FAKE_APPROVAL_REQUEST",
    "LH_WORK_OVERRIDE",
    "LH_PRACTICE_OVERRIDE",
    "LH_PROJECT_DIR",
    "LH_ROLE",
    "LH_UNIT",
    "LH_ROUND",
    "LH_STATION",
    "LH_CLAUDE_BIN",
    "LH_TELEGRAM_API_BASE",
    "LH_LOCK_TOKEN",
    # 使用者電腦上可能用 setx 設了真的 Telegram 金鑰：測試一律拿掉，絕不打真的 Telegram
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


# 安全網：測試預設找不到真的 claude（不存在的執行檔），忘了開假模型模式的測試只會啟動失敗、不會呼叫真模型燒額度。
# 要測 find_claude 本身的測試自己 delenv（見 test_claude_call.py）
NO_REAL_CLAUDE = str(Path(tempfile.gettempdir()) / "lhf-tests-no-real-claude" / "claude.exe")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in LH_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("LH_CLAUDE_BIN", NO_REAL_CLAUDE)


@pytest.fixture
def fake_mode(monkeypatch):
    monkeypatch.setenv("LH_FAKE_MODEL", "1")


@pytest.fixture
def k143_dir(tmp_path):
    """複製 k143 專案（不含 run/）到暫存區，測試產物不會汙染 repo。"""
    target = tmp_path / "k143"
    shutil.copytree(K143_DIR, target, ignore=shutil.ignore_patterns("run"))
    return target


def demo_config():
    """最小的合規專案：producer → verify → qa → review → finish。"""
    return {
        "schema": 1,
        "name": "demo",
        "workdir": "~/lhf-demo-does-not-exist",
        "units": {"ids": ["a", "b", "c"]},
        "files": {
            "out": "{WORK}/{unit}.txt",
            "qa_report": "{RUN}/qa/{unit}.md",
            "rv_report": "{RUN}/rv/{unit}.md",
            "raw": "{RUN}/raw/{unit}.log",
        },
        "stations": [
            {
                "id": "make",
                "kind": "agent",
                "chain": "producer",
                "role": "maker",
                "prompt": "make",
                "outputs": ["{out}"],
            },
            {
                "id": "check",
                "kind": "command",
                "chain": "verify",
                "cwd": "{WORK}",
                "require": ["{out}"],
                "log": "{raw}",
                "fail_report": "{qa_report}",
                "cmds": [{"title": "檢查", "cmd": f'"{PY}" -c "print(1)"', "fail_on_error": True}],
                "on_fail": {"goto": "make"},
            },
            {
                "id": "qa",
                "kind": "agent",
                "chain": "qa",
                "role": "qa",
                "prompt": "qa",
                "clear": ["{qa_report}"],
                "verdict": {"file": "{qa_report}"},
                "on_fail": {"goto": "make"},
            },
            {
                "id": "rv",
                "kind": "agent",
                "chain": "review",
                "role": "rv",
                "prompt": "rv",
                "clear": ["{rv_report}"],
                "verdict": {"file": "{rv_report}"},
                "on_fail": {"goto": "make"},
            },
            {"id": "done", "kind": "commit", "chain": "finish", "paths": ["{unit}.txt"]},
        ],
        "roles": {
            "maker": {"tools": ["Write", "Bash"], "write": ["{out}"], "bash_allow": ["^echo ", "^ssh "]},
            "qa": {"tools": ["Read", "Write"], "write": ["{RUN}/qa/**"]},
            "rv": {"tools": ["Read", "Write"], "write": ["{RUN}/rv/**"], "budget": 2},
        },
        "prompts": {"make": "做 {unit}{feedback}", "qa": "判 {unit}", "rv": "審 {unit}"},
        "forbidden": {"paths": ["protected/src"], "commands": ["git push", "rm -rf"]},
        "approval_commands": ["^ssh "],
    }


def write_project(base, data, name="demo"):
    project = base / name
    (project / "agents").mkdir(parents=True, exist_ok=True)
    for role in data.get("roles") or {}:
        (project / "agents" / f"{role}.md").write_text(f"# {role}\n", encoding="utf-8")
    (project / "factory.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return project


@pytest.fixture
def demo_dir(tmp_path):
    return write_project(tmp_path, demo_config())
