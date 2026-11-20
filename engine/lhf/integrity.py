"""指令站前後的受保護檔比對（過渡措施，在 verify 站沙箱完成之前），以及 hours 每輪之間的控制檔比對。

指令站跑的是撰寫者寫的腳本，沒有經過 gate，理論上能改任何檔案。這裡在站前、站後各拍一次受保護檔的快照：
  引擎檔（kind=engine，保留原始內容，有變動就還原）：run/commands*.jsonl、run/approvals/**、run/state.json、
      run/.runtime/**（不含 RUNTIME_VOLATILE：指令站標記、佇列互斥檔、停止要求）、factory.yaml、agents/**
  工作目錄控制檔（kind=control，只比對雜湊）：.git 目錄的 HEAD／hooks/**，或 worktree 的 gitdir 檔、gitdir/HEAD、
      commondir 及共用目錄的 hooks/**；.gitmodules、.gitattributes、.mcp.json、.claude/**、任何子目錄的 CLAUDE*.md
  git 設定（kind=gitconfig，只比對「危險鍵」）：config／config.worktree 只取 DANGEROUS_KEY_RES 命中的鍵值
      （hooksPath、fsmonitor、sshCommand、*.helper、include*、filter.*、alias.*、editor、pager…）。
      worktree 的共用 config 由主 repo 共用，人在主 repo 做 fetch、建分支、設上游都會改到它；只比對危險鍵才不會誤判。
有任何變動 → 呼叫端把單位標 failed＋ALERT，並還原引擎檔（本站期間新增的佇列指令與核准因此一律不會被套用）；
工作目錄控制檔／git 設定有變動時 run 以 EXIT_CONTROL_CHANGED 結束、hours 停止（見 hours.py）。

人下的 approve／reset 與指令站開站用「佇列互斥檔」序列化（見 queue_mutex）：引擎在互斥檔內放「指令站執行中」
標記；人在互斥檔內確認沒有有效標記才排入。所以排入的指令不是在站前快照之前（會被快照收進去），就是等站結束，
不會在站前快照與站後快照之間出現而被誤判成竄改。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from lhf.common import atomic_write_text, now_str
from lhf.config import CONTROL_FILENAMES, CONTROL_SEGMENTS
from lhf.errors import RoundError

MARKER_NAME = "command-station.json"
QUEUE_MUTEX_NAME = "queue.mutex"
STOP_REQUEST_NAME = "stop-request.json"
CAPABILITY_NAME = "claude-capability.json"
# 這些 .runtime 檔會在指令站執行期間被「人」合法地建立或刪除（stop、排入佇列、另一個終端開 run／hours 時的
# claude 能力檢查快取），不列入引擎檔比對；偽造它們最多讓工廠停下或略過一次 --help 檢查
RUNTIME_VOLATILE = frozenset({MARKER_NAME, QUEUE_MUTEX_NAME, STOP_REQUEST_NAME, CAPABILITY_NAME})
# 掃 CLAUDE.md 時略過的大目錄（建置產物、套件、虛擬環境、ROS 2 colcon 的 build／install／log…）；比對時不分大小寫
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        "build",
        "install",
        "log",
        "logs",
        "dist",
        "target",
        ".next",
        ".nuxt",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".gradle",
        "site-packages",
        "bower_components",
    }
)
# CLAUDE.md 掃描最多走訪幾個資料夾；超過就停止掃描並警告（代理用 --restricted 本來就不載入 CLAUDE.md，
# 這裡只是縱深防禦，不值得為了巨大的工作目錄把每個指令站拖慢幾十秒）
SCAN_DIR_LIMIT = 20000
KEEP_BYTES_LIMIT = 20 * 1024 * 1024  # 引擎檔超過這個大小只記雜湊、不保留內容
MUTEX_STALE_SEC = 30  # 互斥檔超過這麼久還在＝持有者當掉了
MUTEX_POLL_SEC = 0.05
GIT_TIMEOUT_SEC = 30
INCLUDE_DEPTH = 3

# 會讓 git 執行外部程式、改變 hook 位置、改變工作目錄或把資料送到別處的設定鍵（git 輸出的鍵：段名與鍵名小寫）
DANGEROUS_KEY_RES = tuple(
    re.compile(pattern)
    for pattern in (
        r"^core\.(hookspath|fsmonitor|sshcommand|editor|pager|gitproxy|askpass|worktree|bare|attributesfile)$",
        r"\.helper$",
        r"^include\.",
        r"^includeif\.",
        r"^filter\.",
        r"^alias\.",
        r"^pager\.",
        r"^sequence\.editor$",
        r"^diff\.external$",
        r"^diff\..+\.(textconv|command)$",
        r"^merge\..+\.driver$",
        r"^(difftool|mergetool)\..+\.(cmd|path)$",
        r"^gpg\.(.+\.)?program$",
        r"^credential\.",
        r"^http\.",
        r"^url\.",
        r"^remote\..+\.(url|pushurl|uploadpack|receivepack|proxy|vcs)$",
        r"^submodule\..+\.update$",
        r"^ssh\.variant$",
        r"^extensions\.",
        r"^uploadpack\.packobjectshook$",
        r"^protocol\.",
        r"^(web|browser|man)\.",
        r"^instaweb\.",
        r"^sendemail\.",
    )
)
_WARNED = set()


def warn_once(key, text):
    if key not in _WARNED:
        _WARNED.add(key)
        print(f"（警告：{text}）", file=sys.stderr, flush=True)


# ---------- 指令站標記與佇列互斥檔 ----------


def marker_path(cfg):
    return cfg.run_dir / ".runtime" / MARKER_NAME


def mutex_path(cfg):
    return cfg.run_dir / ".runtime" / QUEUE_MUTEX_NAME


@contextmanager
def queue_mutex(cfg, timeout=60.0):
    """「人排入佇列」與「引擎放指令站標記」共用的互斥檔（O_EXCL 建立）；超過 MUTEX_STALE_SEC 的殘留檔自動清除。"""
    path = mutex_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):
            age = file_age(path)
            if age is not None and age > MUTEX_STALE_SEC:
                safe_unlink(path)
                continue
            if time.monotonic() > deadline:
                raise RoundError(f"等了 {timeout:.0f} 秒還拿不到佇列互斥檔 {path.name}，請稍後再試") from None
            time.sleep(MUTEX_POLL_SEC)
    try:
        yield
    finally:
        safe_unlink(path)


def file_age(path):
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def safe_unlink(path):
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        pass  # Windows：別人正開著或也在刪；下一次再處理


def set_marker(cfg, rt, station):
    """標記記下「我是誰（pid＋啟動時間）」與「當時持鎖的是誰」；人工指令用它判斷標記是不是殘留的。"""
    from lhf import lock

    owner = lock.read_lock(cfg) or {}
    record = {
        "unit": rt["unit"],
        "station": station["id"],
        "since": now_str(),
        "pid": os.getpid(),
        "started": lock.process_start_time(os.getpid()),
        "lock_pid": owner.get("pid"),
    }
    atomic_write_text(marker_path(cfg), json.dumps(record, ensure_ascii=False))


def clear_marker(cfg):
    marker_path(cfg).unlink(missing_ok=True)


def read_marker(cfg):
    try:
        data = json.loads(marker_path(cfg).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}  # 存在但讀不懂：視為無效（殘留或寫到一半）
    return data if isinstance(data, dict) else {}


def marker_valid(cfg, marker):
    """有效＝標記的程序還活著（pid＋啟動時間相符）、執行鎖還活著，且標記記的持鎖者就是現在的持鎖程序。"""
    from lhf import lock

    if not marker or not isinstance(marker.get("pid"), int):
        return False
    started = lock.process_start_time(marker["pid"])
    if started is None:
        return False
    if marker.get("started") not in (None, 0) and started not in (0, marker["started"]):
        return False  # PID 被別的程序重用
    owner = lock.read_lock(cfg)
    if not lock.lock_alive(owner):
        return False
    return "lock_pid" not in marker or marker["lock_pid"] == owner.get("pid")


def active_marker(cfg):
    """回傳有效的指令站標記；沒有或是殘留的回 None。"""
    marker = read_marker(cfg)
    return marker if marker_valid(cfg, marker) else None


def clear_foreign_marker(cfg):
    """持鎖程序在每輪開始（還沒有任何指令站在跑）呼叫：清掉不是自己放的標記（上一輪當掉留下的）。"""
    marker = read_marker(cfg)
    if marker is None or marker.get("pid") == os.getpid():
        return False
    safe_unlink(marker_path(cfg))
    print(f"    （清除殘留的指令站標記：PID {marker.get('pid', '?')}，站 {marker.get('station', '?')}）", flush=True)
    return True


# ---------- 要比對的檔案 ----------


def files_under(root):
    root = Path(root)
    if root.is_file():
        return [root]
    return [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []


def engine_files(cfg):
    run = cfg.run_dir
    paths = list(run.glob("commands*.jsonl")) + files_under(run / "approvals") + files_under(cfg.agents_dir)
    paths += [run / "state.json", cfg.project_dir / "factory.yaml"]
    paths += [
        p for p in files_under(run / ".runtime") if p.name not in RUNTIME_VOLATILE and not p.name.endswith(".tmp")
    ]
    return paths


def git_targets(work):
    """回傳 [(路徑, kind)]：.git 是目錄就看 HEAD／config／hooks；是 worktree 的 gitdir 檔就追到它指的目錄與共用目錄。"""
    dot_git = Path(work) / ".git"
    if dot_git.is_dir():
        targets = [(dot_git / "HEAD", "control"), (dot_git / "config", "gitconfig")]
        targets += [(dot_git / "config.worktree", "gitconfig")]
        return targets + [(p, "control") for p in files_under(dot_git / "hooks")]
    if not dot_git.is_file():
        return []
    targets = [(dot_git, "control")]
    text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    if text.lower().startswith("gitdir:"):
        gitdir = Path(text.split(":", 1)[1].strip())
        gitdir = gitdir if gitdir.is_absolute() else (Path(work) / gitdir)
        targets += [(gitdir / "HEAD", "control"), (gitdir / "commondir", "control")]
        targets += [(gitdir / "config", "gitconfig"), (gitdir / "config.worktree", "gitconfig")]
        common = gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
        if common != gitdir:
            targets += [(common / "config", "gitconfig"), (common / "config.worktree", "gitconfig")]
        targets += [(p, "control") for p in files_under(common / "hooks")]
    return targets


def git_files(work):
    """只要路徑（給測試與診斷用）。"""
    return [path for path, _ in git_targets(work)]


def claude_md_files(base):
    """base 底下任何子目錄的 CLAUDE.md／CLAUDE.local.md；略過 SKIP_DIRS，最多走訪 SCAN_DIR_LIMIT 個資料夾。
    依名稱排序走訪，同一個工作目錄每次掃到的範圍一致（超過上限時也一樣）。"""
    found = []
    for visited, (directory, subdirs, names) in enumerate(os.walk(base), 1):
        if visited > SCAN_DIR_LIMIT:
            warn_once(
                ("scan", str(base)),
                f"{base} 底下資料夾超過 {SCAN_DIR_LIMIT} 個，CLAUDE.md 掃描只看前 {SCAN_DIR_LIMIT} 個"
                "（大目錄請放進 build／dist 等會略過的名稱）",
            )
            break
        subdirs[:] = sorted(d for d in subdirs if d.lower() not in SKIP_DIRS)
        found += [Path(directory) / n for n in sorted(names) if n.lower() in CONTROL_FILENAMES]
    return found


def work_targets(cfg):
    targets = []
    for base in dict.fromkeys(cfg.control_bases()):
        root = Path(base)
        targets += git_targets(root)
        targets += [(root / name, "control") for name in sorted(CONTROL_SEGMENTS - {".git", ".claude"})]
        targets += [(p, "control") for p in files_under(root / ".claude") + claude_md_files(root)]
    return targets


# ---------- git 設定的危險鍵 ----------


def find_git():
    return shutil.which("git")


def git_config_entries(path):
    """用 git 自己解析（不自己寫 parser，避免「我們讀到的」與「git 實際讀到的」不同）；回傳 [(鍵, 值)] 或 None。"""
    git = find_git()
    if git is None:
        warn_once("nogit", "找不到 git，git 設定改為整檔比對")
        return None
    try:
        result = subprocess.run(
            [git, "config", "--file", str(path), "--no-includes", "--list", "-z"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None  # git 讀不懂：呼叫端改用整檔雜湊（fail closed，任何改動都看得到）
    entries = []
    for item in result.stdout.decode("utf-8", "replace").split("\0"):
        if item:
            key, _, value = item.partition("\n")
            entries.append((key.lower(), value))
    return entries


def is_dangerous_key(key):
    return any(pattern.search(key) for pattern in DANGEROUS_KEY_RES)


def include_target(config_path, value):
    expanded = os.path.expanduser(value.strip())
    target = Path(expanded)
    return target if target.is_absolute() else Path(config_path).parent / target


def dangerous_lines(path, depth=0, seen=None):
    """config 裡危險鍵的「鍵=值」行（照 git 的順序，後面的會覆蓋前面）；include 的檔一併展開（最多 3 層）。"""
    seen = set() if seen is None else seen
    key = os.path.normcase(os.path.abspath(path))
    if key in seen:
        return [f"（循環 include：{path}）"]
    seen.add(key)
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return [f"（讀不到：{exc}）"]
    entries = git_config_entries(path)
    if entries is None:
        return [f"（git 無法解析，整檔雜湊 {hashlib.sha256(raw).hexdigest()}）"]
    lines = []
    for name, value in entries:
        if not is_dangerous_key(name):
            continue
        lines.append(f"{name}={value}")
        if name.endswith(".path") and (name.startswith(("include.", "includeif."))):
            lines += include_lines(path, value, depth, seen)
    return lines


def include_lines(path, value, depth, seen):
    target = include_target(path, value)
    if depth >= INCLUDE_DEPTH:
        return [f"（include 超過 {INCLUDE_DEPTH} 層，未展開：{target}）"]
    nested = dangerous_lines(target, depth + 1, seen)
    if nested is None:
        return [f"  [{target}]（不存在）"]
    return [f"  [{target}] {line}" for line in nested]


# ---------- 快照與比對 ----------


def read_entry(path, kind):
    """回傳 {sha, data, kind}；檔案不存在回 None。gitconfig 的 sha 只涵蓋危險鍵。"""
    if kind == "gitconfig":
        lines = dangerous_lines(path)
        if lines is None:
            return None
        return {"sha": hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest(), "data": None, "kind": kind}
    try:
        data = path.read_bytes()
    except OSError:
        return None
    keep = kind == "engine" and len(data) <= KEEP_BYTES_LIMIT
    return {"sha": hashlib.sha256(data).hexdigest(), "data": data if keep else None, "kind": kind}


def collect(targets):
    result = {}
    for path, kind in targets:
        key = str(path)
        if key in result:
            continue
        entry = read_entry(path, kind)
        if entry is not None:
            result[key] = entry
    return result


def snapshot(cfg):
    """回傳 {路徑: {sha, data, kind}}；檔案不存在就不列入（新增的檔案在 diff 時會出現）。"""
    targets = [(p, "engine") for p in engine_files(cfg)] + work_targets(cfg)
    return collect(targets)


def control_snapshot(cfg):
    """只含工作目錄控制檔與 git 設定（hours 每輪之間比對用；引擎檔在輪與輪之間本來就會變）。"""
    return collect(work_targets(cfg))


def diff(before, after):
    changed = [p for p in after if p not in before or after[p]["sha"] != before[p]["sha"]]
    changed += [p for p in before if p not in after]
    return sorted(set(changed))


def kind_of(path_text, before, after):
    entry = after.get(path_text) or before.get(path_text) or {}
    return entry.get("kind", "engine")


def control_changes(before, after, changed):
    """changed 裡屬於工作目錄控制檔或 git 設定的路徑。"""
    return [p for p in changed if kind_of(p, before, after) in ("control", "gitconfig")]


def describe_control(before, after, changed):
    """給人看的說明：哪些控制檔變了；git 設定／共用 hooks 變動時提醒「可能是人在主 repo 操作」。"""
    items = control_changes(before, after, changed)
    if not items:
        return ""
    labels = [f"{p}（危險 git 設定鍵）" if kind_of(p, before, after) == "gitconfig" else p for p in items[:10]]
    more = f" 等 {len(items)} 個" if len(items) > 10 else ""
    text = f"工作目錄控制檔被改動：{'、'.join(labels)}{more}"
    if any(kind_of(p, before, after) == "gitconfig" or "hooks" in Path(p).parts for p in items):
        text += "（worktree 的 git 設定與 hooks 跟主 repo 共用，可能是人在主 repo 操作；確認安全後才重新啟動）"
    return text


def restore(before, changed):
    """把有保留原始內容的引擎檔還原成站前的樣子；站前不存在的引擎檔刪除。工作目錄檔不動（交給人處理）。"""
    restored = []
    for path_text in changed:
        path, entry = Path(path_text), before.get(path_text)
        try:
            if entry is None:
                if is_engine_owned(path_text, before):
                    path.unlink(missing_ok=True)
                    restored.append(path_text)
            elif entry["data"] is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(entry["data"])
                restored.append(path_text)
        except OSError:
            continue
    return restored


def is_engine_owned(path_text, before):
    """站前不存在的新檔：只在它落在引擎資料夾（有保留內容的檔所在處）時才刪。"""
    roots = {str(Path(p).parent) for p, e in before.items() if e["data"] is not None}
    return any(path_text.startswith(root + os.sep) or path_text.startswith(root + "/") for root in roots)
