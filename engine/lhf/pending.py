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

from lhf.common import EXIT_OK, EXIT_UNIT_FAILED, scrubbed_env
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
    "-c",
    "submodule.recurse=false",
)


def raw_git(work, args, prefix=SAFE_GIT_ARGS):
    try:
        result = subprocess.run(
            ["git", *prefix, "-C", str(work), *args],
            capture_output=True,
            env=scrubbed_env(),
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
    items, paths = (
        split_z(git(work, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"])),
        [],
    )
    index = 0
    while index < len(items):
        entry = items[index]
        paths.append(entry[3:])
        if entry[:1] in ("R", "C"):
            index += 1  # 跳過舊路徑
        index += 1
    return paths


def upstream_paths(work, notes):
    """有上游分支：上游之後的 commit 改到的檔（推上去時會一起上去的全部）。"""
    upstream = git(work, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not upstream:
        notes.append(
            "沒有上游分支：只能列出工作目錄目前的改動與本單位 commit 站記下的 commit（推上去時整個分支都會上去）"
        )
        return []
    return split_z(
        git(work, ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", f"{upstream.strip()}...HEAD"])
    )


def unit_commit_paths(work, rec):
    """本單位 commit 站記下的 commit（state 的 commits）改到的檔。"""
    paths = []
    for sha in rec.get("commits") or []:
        paths += split_z(git(work, ["show", "--no-ext-diff", "--no-textconv", "--name-only", "--format=", "-z", sha]))
    return sorted(dict.fromkeys(paths))


def repo_ready(cfg):
    """回傳 (工作目錄, 說明)；說明非空＝不能列改動（假模式或不是 git repo）。"""
    work = cfg.work_dir()
    if is_fake() and not os.environ.get("LH_WORK_OVERRIDE"):
        return work, "假模型模式：工作目錄是暫存區，不列出改動"
    if git(work, ["rev-parse", "--git-dir"]) is None:
        return work, f"{posix(work)} 不是 git repo（或假模型模式），無法列出改動"
    return work, ""


def collect_changes(cfg, rec):
    """回傳 (工作目錄目前的改動＋上游之後的 commit, 本單位 commit 的檔, 說明)。"""
    work, problem = repo_ready(cfg)
    if problem:
        return [], [], [problem]
    notes = []
    workdir = sorted(dict.fromkeys(status_paths(work) + upstream_paths(work, notes)))
    return workdir, unit_commit_paths(work, rec), notes


def changed_files(cfg, rec):
    """兩類改動的聯集（舊介面，測試與診斷用）。"""
    workdir, unit_files, notes = collect_changes(cfg, rec)
    return sorted(set(workdir) | set(unit_files)), notes


def executable_entries(workdir, unit_files):
    sources = {}
    for path in workdir:
        sources.setdefault(path, []).append("workdir")
    for path in unit_files:
        sources.setdefault(path, []).append("unit_commit")
    return [
        {"path": path, "reason": executable_reason(path), "source": "＋".join(where)}
        for path, where in sorted(sources.items())
        if executable_reason(path)
    ]


def load_rec(cfg, unit):
    state = load_state(cfg)
    if state is None or unit not in state["units"]:
        raise RoundError(f"{unit} 不在 state.json")
    return state["units"][unit]


def pending_info(cfg, unit):
    rec = load_rec(cfg, unit)
    station_id = (rec.get("approval") or {}).get("station") or rec["station"]
    station = cfg.station(station_id)
    requests = [r for r in read_jsonl(pending_approvals_path(cfg)) if r.get("unit") == unit]
    workdir, unit_files, notes = collect_changes(cfg, rec)
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
        "workdir_changes": workdir,
        "workdir_changes_note": "工作目錄目前的改動（未 commit、未追蹤）＋上游分支之後的 commit；不一定都是本單位改的",
        "unit_commits": rec.get("commits") or [],
        "unit_commit_files": unit_files,
        "executable_files": executable_entries(workdir, unit_files),
        "notes": notes,
        "approve_command": f"python -m lhf {posix(Path(cfg.project_dir))} approve {unit}",
    }


def cmd_pending_info(cfg, unit, diff_path=None):
    if diff_path is not None:
        return cmd_safe_diff(cfg, unit, diff_path)
    info = sanitize(pending_info(cfg, unit))
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return EXIT_OK if info["pending"] else EXIT_UNIT_FAILED


# ---------- pending-info --diff：引擎輸出安全的 diff ----------

DIFF_MAX_LINES = 400
# 終端機控制字元與雙向文字控制字元：撰寫者可以用它們讓顯示的內容與實際內容不同（例如把指令藏起來）
BIDI_CHARS = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C, *range(0x200B, 0x200E), 0x2028, 0x2029, 0xFEFF)}  # 含零寬與行分隔字元


def escape_display(text):
    """控制字元（換行、tab 除外）與雙向控制字元一律轉成可見的 \\xNN／\\uNNNN。"""
    out = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) < 0xA0:
            out.append(f"\\x{ord(ch):02x}")
        elif ch in BIDI_CHARS:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def sanitize(value):
    """JSON 本身會跳脫 < 0x20 的控制字元，但不會跳脫雙向控制字元與 C1 控制字元：字串一律過 escape_display。"""
    if isinstance(value, str):
        return escape_display(value)
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    return value


def safe_rel_path(work, rel):
    """只接受工作目錄底下的相對路徑（不可絕對路徑、不可含 ..）。"""
    text = str(rel).replace("\\", "/")
    if not text or text.startswith("/") or (len(text) > 1 and text[1] == ":") or ".." in text.split("/"):
        raise RoundError(f"--diff 只接受工作目錄底下的相對路徑：{rel!r}")
    target = (Path(work) / text).resolve()
    if not Path(os.path.normcase(target)).is_relative_to(os.path.normcase(Path(work).resolve())):  # 字串前綴會放行 <work>-evil
        raise RoundError(f"--diff 的路徑跑出工作目錄：{rel!r}")
    return text


def diff_sections(work, rec, rel):
    flags = ["--no-ext-diff", "--no-textconv", "--no-color"]
    sections = [("未 commit 的改動（相對於 HEAD）", git(work, ["diff", *flags, "HEAD", "--", rel]) or "")]
    upstream = git(work, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if upstream:
        sections.append(
            (
                f"上游 {upstream.strip()} 之後的 commit",
                git(work, ["diff", *flags, f"{upstream.strip()}...HEAD", "--", rel]) or "",
            )
        )
    for sha in rec.get("commits") or []:
        sections.append(
            (f"本單位 commit {sha[:12]}", git(work, ["show", *flags, "--format=%h %s", sha, "--", rel]) or "")
        )
    tracked = git(work, ["ls-files", "--", rel])
    if not tracked and (Path(work) / rel).is_file():
        content = (Path(work) / rel).read_text(encoding="utf-8", errors="replace")
        sections.append(("未追蹤的新檔（內容）", content))
    return sections


def cmd_safe_diff(cfg, unit, rel):
    """不執行 repo 設定裡任何程式的 diff，輸出前跳脫控制字元，最多 DIFF_MAX_LINES 行。"""
    rec = load_rec(cfg, unit)
    work, problem = repo_ready(cfg)
    if problem:
        print(problem)
        return EXIT_UNIT_FAILED
    rel = safe_rel_path(work, rel)
    lines = []
    for title, body in diff_sections(work, rec, rel):
        lines += [f"===== {title}：{rel} =====", *(body.splitlines() or ["（無）"])]
    shown = lines[:DIFF_MAX_LINES]
    print(escape_display("\n".join(shown)))
    if len(lines) > DIFF_MAX_LINES:
        print(f"…（共 {len(lines)} 行，只顯示前 {DIFF_MAX_LINES} 行）")
    return EXIT_OK


# ---------- verify-commits：換電腦後確認 state 記的 commit 都在工作目錄 ----------


def cmd_verify_commits(cfg):
    """每個單位 state 裡記的 commit 都必須存在、且是工作目錄 HEAD 的祖先；少任何一個就結束碼 1。"""
    state = load_state(cfg)
    if state is None:
        raise RoundError("沒有 state.json，先執行 init")
    work, problem = repo_ready(cfg)
    if problem:
        print(problem)
        return EXIT_UNIT_FAILED
    missing, checked = [], 0
    for unit in state["queue"]:
        for sha in state["units"][unit].get("commits") or []:
            checked += 1
            exists = git(work, ["cat-file", "-e", f"{sha}^{{commit}}"]) is not None
            on_branch = exists and git(work, ["merge-base", "--is-ancestor", sha, "HEAD"]) is not None
            if not on_branch:
                missing.append((unit, sha, "不存在" if not exists else "不在目前分支上"))
    for unit, sha, why in missing:
        print(f"  ✗ {unit} {sha[:12]}：{why}")
    print(f"檢查 {checked} 個 commit，缺 {len(missing)} 個（工作目錄 {posix(work)}）")
    return EXIT_UNIT_FAILED if missing else EXIT_OK
