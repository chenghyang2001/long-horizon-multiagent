"""pending-info <id>：核准前給人（skill）看的待核准內容，輸出 JSON，唯讀（不拿鎖、不改任何檔）。

內容：單位狀態、停在哪一站、站型核准或指令核准、待核准紀錄，以及本單位改到的檔裡
「推上去會被執行」的檔（CI 設定、package.json scripts、Makefile、shell 腳本、Dockerfile、git hooks…）。
改到的檔＝工作目錄未提交的改動＋（有上游分支時）上游之後的 commit；沒有上游時改看本單位 commit 站記下的 commit。
結束碼：0＝單位正在等待核准；1＝單位沒有在等核准（照樣輸出 JSON，方便呼叫端顯示原因）。
"""

import fnmatch
import json
import os
import subprocess
from pathlib import Path

from lhf.common import EXIT_OK, EXIT_UNIT_FAILED
from lhf.config import is_fake, posix
from lhf.errors import RoundError
from lhf.state import PENDING_APPROVAL, load_state, pending_approvals_path, read_jsonl

GIT_TIMEOUT_SEC = 60
# (pattern, 原因)；pattern 以 / 結尾＝該資料夾底下全部；含 / ＝比對完整相對路徑；否則比對檔名。一律不分大小寫
EXECUTABLE_RULES = (
    (".github/", "GitHub Actions／GitHub 設定（push 後在 CI 執行）"),
    (".gitlab-ci.yml", "GitLab CI"),
    (".circleci/", "CircleCI"),
    ("azure-pipelines.yml", "Azure Pipelines"),
    ("bitbucket-pipelines.yml", "Bitbucket Pipelines"),
    (".travis.yml", "Travis CI"),
    ("jenkinsfile", "Jenkins"),
    (".husky/", "git hooks（husky）"),
    (".githooks/", "git hooks"),
    (".pre-commit-config.yaml", "pre-commit hooks"),
    (".devcontainer/", "Dev Container（開容器時執行）"),
    (".vscode/tasks.json", "VS Code 工作（開啟或建置時執行）"),
    ("package.json", "npm scripts（npm install／test／build 會執行，請檢查 scripts 區段）"),
    ("makefile", "Makefile"),
    ("gnumakefile", "Makefile"),
    ("*.mk", "Makefile 片段"),
    ("justfile", "just 工作"),
    ("taskfile.yml", "Task 工作"),
    ("*.sh", "shell 腳本"),
    ("*.bash", "shell 腳本"),
    ("*.zsh", "shell 腳本"),
    ("*.ps1", "PowerShell 腳本"),
    ("*.psm1", "PowerShell 模組"),
    ("*.bat", "批次檔"),
    ("*.cmd", "批次檔"),
    ("dockerfile*", "Dockerfile（建置映像時執行）"),
    ("*.dockerfile", "Dockerfile"),
    ("docker-compose*.yml", "docker compose"),
    ("docker-compose*.yaml", "docker compose"),
    ("compose*.yml", "docker compose"),
    ("compose*.yaml", "docker compose"),
    ("setup.py", "Python 安裝腳本"),
    ("pyproject.toml", "Python 建置設定"),
    ("setup.cfg", "Python 建置設定"),
    ("tox.ini", "tox"),
    ("noxfile.py", "nox"),
    ("conftest.py", "pytest 會自動載入"),
    ("cmakelists.txt", "CMake"),
    ("*.cmake", "CMake"),
    ("build.gradle*", "Gradle"),
    ("settings.gradle*", "Gradle"),
    ("pom.xml", "Maven"),
    ("cargo.toml", "Cargo"),
    ("build.rs", "Cargo 建置腳本"),
    ("vercel.json", "Vercel 部署設定"),
    ("netlify.toml", "Netlify 部署設定"),
    ("procfile", "部署啟動指令"),
    ("*.service", "systemd 服務"),
    ("*.timer", "systemd 排程"),
    ("crontab", "cron 排程"),
    (".envrc", "direnv（進目錄就執行）"),
    (".gitattributes", "git 屬性（可指定 filter 驅動程式）"),
    (".gitmodules", "git 子模組"),
)


def executable_reason(rel_path):
    """推上去（或別人 pull 下來）會被執行的檔回傳原因；一般檔回 None。"""
    text = posix(rel_path).lower()
    name = text.rsplit("/", 1)[-1]
    for pattern, reason in EXECUTABLE_RULES:
        if pattern.endswith("/"):
            if text.startswith(pattern) or f"/{pattern}" in f"/{text}":
                return reason
        elif "/" in pattern:
            if text == pattern or text.endswith("/" + pattern):
                return reason
        elif fnmatch.fnmatchcase(name, pattern):
            return reason
    return None


# 核准前看改動的這一刻，工作目錄可能已被撰寫者動過手腳：git status／diff 不可執行 repo 設定裡的外部程式
# （fsmonitor、hooks、外部 diff、clean／smudge filter）。命令列的 -c 優先於 repo 設定。
SAFE_GIT_ARGS = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "diff.external=",
    "-c",
    "core.pager=cat",
    "-c",
    "core.untrackedCache=false",
)


def raw_git(work, args, prefix=SAFE_GIT_ARGS):
    try:
        result = subprocess.run(
            ["git", *prefix, "-C", str(work), *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else None


def filter_overrides(work):
    """repo 設定裡每個 filter 驅動程式都覆寫成空（git 遇到空的 clean／smudge／process 就不跑）；只讀設定、不執行。"""
    listing = raw_git(work, ["config", "--name-only", "--get-regexp", r"^filter\."]) or ""
    names = {key[len("filter.") : key.rfind(".")] for key in listing.splitlines() if key.count(".") >= 2}
    args = []
    for name in sorted(names):
        for field, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
            args += ["-c", f"filter.{name}.{field}={value}"]
    return args


def git(work, args):
    prefix = [*SAFE_GIT_ARGS, *filter_overrides(work)]
    return raw_git(work, args, prefix=prefix)


def split_z(text):
    return [item for item in (text or "").split("\0") if item]


def status_paths(work):
    """git status --porcelain -z：一般是「XY 路徑」；改名／複製是「XY 新路徑」接著一筆「舊路徑」。"""
    items, paths = split_z(git(work, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])), []
    index = 0
    while index < len(items):
        entry = items[index]
        paths.append(entry[3:])
        if entry[:1] in ("R", "C"):
            index += 1  # 跳過舊路徑
        index += 1
    return paths


def committed_paths(work, rec, notes):
    upstream = git(work, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if upstream:
        return split_z(
            git(work, ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", f"{upstream.strip()}...HEAD"])
        )
    notes.append("沒有上游分支：改列本單位 commit 站記下的 commit（推上去時整個分支的其他 commit 也會一起上去）")
    paths = []
    for sha in rec.get("commits") or []:
        paths += split_z(git(work, ["show", "--no-ext-diff", "--no-textconv", "--name-only", "--format=", "-z", sha]))
    return paths


def changed_files(cfg, rec):
    """回傳 (改到的檔, 說明)；工作目錄不是 git repo（或假模式）時回空清單並說明。"""
    work = cfg.work_dir()
    notes = []
    if is_fake() and not os.environ.get("LH_WORK_OVERRIDE"):
        return [], ["假模型模式：工作目錄是暫存區，不列出改動"]
    if git(work, ["rev-parse", "--git-dir"]) is None:
        return [], [f"{posix(work)} 不是 git repo（或假模型模式），無法列出改動"]
    paths = status_paths(work) + committed_paths(work, rec, notes)
    return sorted(dict.fromkeys(paths)), notes


def pending_info(cfg, unit):
    state = load_state(cfg)
    if state is None or unit not in state["units"]:
        raise RoundError(f"{unit} 不在 state.json")
    rec = state["units"][unit]
    station_id = (rec.get("approval") or {}).get("station") or rec["station"]
    station = cfg.station(station_id)
    requests = [r for r in read_jsonl(pending_approvals_path(cfg)) if r.get("unit") == unit]
    files, notes = changed_files(cfg, rec)
    executable = [{"path": p, "reason": executable_reason(p)} for p in files if executable_reason(p)]
    kinds = sorted({r.get("kind") or "command" for r in requests})
    return {
        "project": cfg.name,
        "unit": unit,
        "status": rec["status"],
        "pending": rec["status"] == PENDING_APPROVAL,
        "station": {"id": station["id"], "kind": station["kind"], "label": station.get("label") or station["id"]},
        "approval_kind": "／".join(kinds) or None,
        "reason": (rec.get("approval") or {}).get("reason"),
        "requests": [{k: r.get(k) for k in ("ts", "kind", "role", "station", "round", "command")} for r in requests],
        "workdir": posix(cfg.work_dir()),
        "branch": cfg.data.get("branch"),
        "changed_files": files,
        "executable_files": executable,
        "notes": notes,
        "approve_command": f"python -m lhf {posix(Path(cfg.project_dir))} approve {unit}",
    }


def cmd_pending_info(cfg, unit):
    info = pending_info(cfg, unit)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return EXIT_OK if info["pending"] else EXIT_UNIT_FAILED
