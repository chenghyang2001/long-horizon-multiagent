"""呼叫 claude -p 的唯一入口：找執行檔、組環境變數與命令列、重試、解析輸出、確認 gate 生效。

Windows 兩個坑（i5 實際踩過）：
1. npm 版 claude 是 claude.CMD，cmd.exe 解析批次檔參數遇到換行就截斷，後面的 --settings（gate hook）
   整段消失 → 優先用 claude.exe 或 node 入口，長內容一律不上命令列，並由 ensure_single_line_args 把關。
2. --tools 要用 variadic（"--tools", *tools），不要逗號串接。
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from lhf import fake
from lhf.config import ENGINE_DIR, is_fake, posix, render, render_path
from lhf.errors import RoundError
from lhf.state import file_offset, now_str, read_jsonl_since, run_path, trace_path

LAUNCH_FAIL_HINTS = ("not found", "not recognized")
# 巢狀 session 變數會讓子 claude 以為自己在別的 session 裡；以下幾個是正常設定要保留
KEEP_CLAUDE_ENV = {"CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
NODE_ENTRY = Path("node_modules") / "@anthropic-ai" / "claude-code" / "cli.js"
# 平行站的多個執行緒會同時重寫 hook 設定檔；Windows 上同時寫同一檔可能 PermissionError
HOOK_LOCK = threading.Lock()


# ---------- 找 claude ----------


def find_claude():
    """回傳命令前綴（list）。順序：LH_CLAUDE_BIN → ~/.local/bin/claude.exe → claude.exe → claude（.CMD 改走 node）。"""
    override = os.environ.get("LH_CLAUDE_BIN")
    if override:
        return [override]
    if os.name == "nt":
        native = Path.home() / ".local" / "bin" / "claude.exe"
        if native.is_file():
            return [str(native)]
        found_exe = shutil.which("claude.exe")
        if found_exe:
            return [found_exe]
    found = shutil.which("claude")
    if found is None:
        raise RoundError("找不到 claude 執行檔（~/.local/bin/claude.exe、PATH 上的 claude 都沒有）")
    if found.lower().endswith((".cmd", ".bat")):
        return node_entry(Path(found)) or [found]
    return [found]


def node_entry(cmd_path):
    """npm 全域安裝時 claude.CMD 旁邊有 node_modules/@anthropic-ai/claude-code/cli.js。"""
    node = shutil.which("node")
    cli = cmd_path.parent / NODE_ENTRY
    if node and cli.is_file():
        return [node, str(cli)]
    print(f"    （警告：只找到 {cmd_path.name}，長參數可能被 cmd.exe 截斷；已啟用單行參數檢查）")
    return None


# ---------- 環境與命令列 ----------


def to_msys(path):
    """C:/x/y → /c/x/y；ORIGINAL_PATH 是冒號分隔，Windows 磁碟代號的冒號會把它切壞。"""
    text = posix(path)
    match = re.match(r"^([A-Za-z]):/(.*)$", text)
    return f"/{match.group(1).lower()}/{match.group(2)}" if match else text


def strip_nested_env(env):
    env.pop("ANTHROPIC_API_KEY", None)  # 成本鐵律：一律走 Max 訂閱
    for key in list(env):
        if key in KEEP_CLAUDE_ENV:
            continue
        if key in ("CLAUDECODE", "CLAUDE_PID") or key.startswith("CLAUDE_CODE_"):
            env.pop(key)


def build_env(cfg, rt, role=None, station_id=None):
    """移除 API key 與巢狀 session 變數，補上專案 env、引擎位置、角色與 python 路徑。"""
    env = os.environ.copy()
    strip_nested_env(env)
    py_dir = Path(sys.executable).parent
    env["PATH"] = str(py_dir) + os.pathsep + env.get("PATH", "")
    if env.get("ORIGINAL_PATH"):
        # Claude 的 Bash 工具用登入 shell 依 ORIGINAL_PATH 重組 PATH，不補就找不到 python
        env["ORIGINAL_PATH"] = to_msys(py_dir) + ":" + env["ORIGINAL_PATH"]
    env["PYTHONPATH"] = str(ENGINE_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    mapping = cfg.vars(unit=rt["unit"], round_no=rt["round"])
    env.update({key: render(value, mapping) for key, value in (cfg.data.get("env") or {}).items()})
    env.update(
        {"LH_PROJECT_DIR": str(cfg.project_dir), "LH_UNIT": rt["unit"], "LH_ROUND": str(rt["round"]), "PYTHONUTF8": "1"}
    )
    for key, value in (("LH_ROLE", role), ("LH_STATION", station_id)):
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    return env


def ensure_single_line_args(cmd):
    """cmd.exe 遇到換行會截斷後面所有參數；任何參數含換行一律拒絕。"""
    for arg in cmd:
        text = str(arg)
        if "\n" in text or "\r" in text:
            raise RoundError(f"claude 命令列參數含換行（會被 cmd.exe 截斷）：{text[:80]!r}")
    return cmd


def add_dirs(cfg, mapping):
    dirs = [posix(cfg.run_dir)] + [render_path(item, mapping) for item in cfg.data.get("read_dirs") or []]
    return list(dict.fromkeys(dirs))


def build_claude_cmd(cfg, role, prefix, hook_path, mapping):
    """長內容一律不上命令列：prompt 走 stdin、角色說明走 --append-system-prompt-file。"""
    tools = [str(tool) for tool in cfg.roles[role]["tools"]]
    cmd = [
        *prefix,
        "-p",
        "--append-system-prompt-file",
        posix(cfg.agents_dir / f"{role}.md"),
        "--tools",
        *tools,
        "--settings",
        posix(hook_path),
        "--setting-sources",
        "project,local",
    ]
    for directory in add_dirs(cfg, mapping):
        cmd += ["--add-dir", directory]
    cmd += ["--output-format", "json"]
    return ensure_single_line_args(cmd)


def write_hook_settings(cfg):
    """每次執行都重寫，確保 python、gate.py、專案路徑跟著這台電腦走。"""
    py = posix(sys.executable)
    gate = posix(ENGINE_DIR / "lhf" / "gate.py")
    hook = f'"{py}" "{gate}" --project "{posix(cfg.project_dir)}"'
    settings = {
        "hooks": {
            "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": hook}]}],
            "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": hook + " --post"}]}],
        }
    }
    path = run_path(cfg, ".runtime") / "hook-settings.json"
    text = json.dumps(settings, ensure_ascii=False, indent=2)
    with HOOK_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    return path


# ---------- 執行 ----------


def is_launch_failure(proc):
    stderr = (proc.stderr or "").lower()
    return proc.returncode != 0 and any(hint in stderr for hint in LAUNCH_FAIL_HINTS)


def run_with_retry(cfg, call):
    """啟動類失敗（找不到指令）等幾秒重試一次；重試不另外扣次數（呼叫前已扣過）。"""
    wait = cfg.limits["launch_retry_wait"]
    for attempt in (1, 2):
        try:
            proc = subprocess.run(
                call["cmd"],
                input=call["prompt"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=cfg.limits["agent_timeout"],
                env=call["env"],
                cwd=call["cwd"],
                check=False,
            )
        except OSError as exc:
            if attempt == 2:
                raise
            print(f"    （{call['role']} 的 claude 啟動失敗：{exc}，{wait} 秒後重試一次）")
        else:
            if attempt == 2 or not is_launch_failure(proc):
                return proc
            print(f"    （{call['role']} 的 claude 疑似找不到指令（結束碼 {proc.returncode}），{wait} 秒後重試一次）")
        time.sleep(wait)
    raise AssertionError("unreachable")


def as_text(value):
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def save_failure_log(cfg, call, stdout, stderr, reason):
    """存完整 stdout／stderr：只截前 300 字的錯誤訊息常常看不出真正原因。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = run_path(cfg, "logs") / f"agent-fail-{stamp}-{call['role']}.txt"
    text = (
        f"時間：{now_str()}\n角色：{call['role']}\n原因：{reason}\n"
        f"指令：{shlex.join(str(arg) for arg in call['cmd'])}\n"
        f"prompt：經 stdin 傳入，{len(call['prompt'])} 字（內容不記錄）\n\n"
        f"===== stdout（{len(stdout)} 字）=====\n{stdout}\n\n"
        f"===== stderr（{len(stderr)} 字）=====\n{stderr}\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return f"（寫入失敗紀錄 {path.name} 失敗：{exc}）"
    return posix(path)


def invoke(cfg, call):
    try:
        return run_with_retry(cfg, call)
    except subprocess.TimeoutExpired as exc:
        log = save_failure_log(cfg, call, as_text(exc.stdout), as_text(exc.stderr), "逾時")
        raise RoundError(
            f"{call['role']} 的 claude -p 逾時（超過 {cfg.limits['agent_timeout']} 秒；完整輸出：{log}）"
        ) from exc
    except OSError as exc:
        raise RoundError(f"{call['role']} 的 claude -p 無法啟動：{exc}") from exc


def parse_agent_json(role, stdout):
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RoundError(f"{role} 的 claude -p 輸出不是 JSON：{stdout.strip()[:300]}") from exc
    if not isinstance(payload, dict):
        raise RoundError(f"{role} 的 claude -p 輸出格式不對：{stdout.strip()[:300]}")
    if payload.get("is_error"):
        raise RoundError(f"{role} 的 claude -p 回報錯誤：{str(payload.get('result'))[:300]}")
    if payload.get("result") is None:
        raise RoundError(f"{role} 的 claude -p 輸出缺少 result：{stdout.strip()[:300]}")
    try:
        cost = float(payload.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    return {
        "result": payload["result"],
        "session_id": payload.get("session_id", ""),
        "cost": cost,
        "turns": payload.get("num_turns"),
    }


def count_new_decisions(cfg, offset, role, unit):
    """平行站時另一個角色也在寫 trace，所以要比對角色與單位。"""
    return sum(
        1
        for record in read_jsonl_since(trace_path(cfg), offset)
        if record.get("kind") == "decision" and record.get("role") == role and record.get("unit") == unit
    )


def verify_gate_active(cfg, role, unit, offset):
    """fail closed：每個角色都一定要用工具寫檔，trace 卻沒有它的決策紀錄＝hook 沒掛上、控制層被繞過。"""
    if count_new_decisions(cfg, offset, role, unit) == 0:
        raise RoundError(f"gate 沒有生效（trace 無 {role} 在 {unit} 的決策紀錄），本站結果不採用")


def prepare_call(cfg, rt, role, prompt, station_id):
    mapping = cfg.vars(unit=rt["unit"], round_no=rt["round"])
    cwd = Path(render_path(cfg.data.get("agent_cwd") or "{WORK}", mapping))
    if not cwd.is_dir():
        raise RoundError(f"代理工作目錄不存在：{posix(cwd)}（檢查 workdir／agent_cwd）")
    cmd = build_claude_cmd(cfg, role, find_claude(), write_hook_settings(cfg), mapping)
    return {"role": role, "cmd": cmd, "prompt": prompt, "cwd": str(cwd), "env": build_env(cfg, rt, role, station_id)}


def call_agent(cfg, rt, station, job):
    """所有 claude -p 呼叫集中在這裡。job＝{role, prompt(已展開), outputs}；回傳 result／session_id／cost／turns。"""
    role = job["role"]
    if is_fake():
        return fake.fake_agent(cfg, rt, station, job)  # 假模式不跑 hook，所以也不做 gate 生效檢查
    call = prepare_call(cfg, rt, role, job["prompt_text"], station["id"])
    offset = file_offset(trace_path(cfg))  # 呼叫前記下位置，之後只看這次呼叫新增的 trace
    proc = invoke(cfg, call)
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    try:
        if proc.returncode != 0:
            raise RoundError(f"{role} 的 claude -p 結束碼 {proc.returncode}：{(stderr or stdout).strip()[:300]}")
        info = parse_agent_json(role, stdout)
    except RoundError as exc:
        log = save_failure_log(cfg, call, stdout, stderr, str(exc))
        raise RoundError(f"{exc}（完整輸出：{log}）") from exc
    verify_gate_active(cfg, role, rt["unit"], offset)
    return info


def describe_cmd(cfg, rt, role):
    """dry-run 用：不找真的 claude、不寫 hook 檔，只組出長相一樣的命令列。"""
    mapping = cfg.vars(unit=rt["unit"], round_no=rt["round"])
    hook = run_path(cfg, ".runtime") / "hook-settings.json"
    return build_claude_cmd(cfg, role, ["claude"], hook, mapping)
