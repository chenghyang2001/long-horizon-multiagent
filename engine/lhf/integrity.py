"""指令站前後的受保護檔比對（過渡措施，在 verify 站沙箱完成之前）。

指令站跑的是撰寫者寫的腳本，沒有經過 gate，理論上能改任何檔案。這裡在站前、站後各拍一次受保護檔的快照：
  引擎檔（保留原始內容，有變動就還原）：run/commands*.jsonl、run/approvals/**、run/state.json、
      run/.runtime/**（不含本站的「指令站執行中」標記）、factory.yaml、agents/**
  工作目錄控制檔（只比對雜湊）：.git（目錄的 HEAD／config／hooks/**，或 worktree 的 gitdir 檔及其
      共用目錄的 config／hooks/**）、.gitmodules、.gitattributes、.mcp.json、.claude/**、任何子目錄的 CLAUDE*.md
有任何變動 → 呼叫端把單位標 failed＋ALERT，並還原引擎檔（本站期間新增的佇列指令與核准因此一律不會被套用）。
人從獨立終端下的 approve／reset 會先等「指令站執行中」標記消失才排入，所以不會被誤判成竄改。
"""

import hashlib
import json
import os
from pathlib import Path

from lhf.common import atomic_write_text, now_str
from lhf.config import CONTROL_FILENAMES, CONTROL_SEGMENTS

MARKER_NAME = "command-station.json"
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox"}
KEEP_BYTES_LIMIT = 20 * 1024 * 1024  # 引擎檔超過這個大小只記雜湊、不保留內容


def marker_path(cfg):
    return cfg.run_dir / ".runtime" / MARKER_NAME


def set_marker(cfg, rt, station):
    atomic_write_text(
        marker_path(cfg),
        json.dumps(
            {"unit": rt["unit"], "station": station["id"], "since": now_str(), "pid": os.getpid()}, ensure_ascii=False
        ),
    )


def clear_marker(cfg):
    marker_path(cfg).unlink(missing_ok=True)


def files_under(root):
    root = Path(root)
    if root.is_file():
        return [root]
    return [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []


def engine_files(cfg):
    run = cfg.run_dir
    paths = list(run.glob("commands*.jsonl")) + files_under(run / "approvals") + files_under(cfg.agents_dir)
    paths += [run / "state.json", cfg.project_dir / "factory.yaml"]
    paths += [p for p in files_under(run / ".runtime") if p.name != MARKER_NAME and not p.name.endswith(".tmp")]
    return paths


def git_files(work):
    """.git 是目錄就看 HEAD／config／hooks；是 worktree 的 gitdir 檔就再追到它指的目錄與共用目錄。"""
    dot_git = Path(work) / ".git"
    if dot_git.is_dir():
        return [dot_git / "HEAD", dot_git / "config", *files_under(dot_git / "hooks")]
    if not dot_git.is_file():
        return []
    paths = [dot_git]
    text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    if text.lower().startswith("gitdir:"):
        gitdir = Path(text.split(":", 1)[1].strip())
        gitdir = gitdir if gitdir.is_absolute() else (Path(work) / gitdir)
        paths += [gitdir / "HEAD", gitdir / "config", gitdir / "commondir"]
        common = gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
        paths += [common / "config", *files_under(common / "hooks")]
    return paths


def claude_md_files(base):
    """base 底下任何子目錄的 CLAUDE.md／CLAUDE.local.md（略過 .git、node_modules 等大目錄）。"""
    found = []
    for directory, subdirs, names in os.walk(base):
        subdirs[:] = [d for d in subdirs if d.lower() not in SKIP_DIRS]
        found += [Path(directory) / n for n in names if n.lower() in CONTROL_FILENAMES]
    return found


def work_files(cfg):
    paths = []
    for base in dict.fromkeys(cfg.control_bases()):
        root = Path(base)
        paths += git_files(root)
        paths += [root / name for name in sorted(CONTROL_SEGMENTS - {".git", ".claude"})]
        paths += files_under(root / ".claude") + claude_md_files(root)
    return paths


def read_entry(path, keep):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    return {"sha": digest, "data": data if keep and len(data) <= KEEP_BYTES_LIMIT else None}


def snapshot(cfg):
    """回傳 {路徑: {sha, data}}；檔案不存在就不列入（新增的檔案在 diff 時會出現）。"""
    result = {}
    for keep, paths in ((True, engine_files(cfg)), (False, work_files(cfg))):
        for path in dict.fromkeys(paths):
            entry = read_entry(path, keep)
            if entry is not None:
                result[str(path)] = entry
    return result


def diff(before, after):
    changed = [p for p in after if p not in before or after[p]["sha"] != before[p]["sha"]]
    changed += [p for p in before if p not in after]
    return sorted(set(changed))


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
