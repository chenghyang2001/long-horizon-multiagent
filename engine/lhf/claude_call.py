"""呼叫 claude -p 的唯一入口：找執行檔、組環境變數與命令列、重試、解析輸出、確認 gate 生效。

Windows 的坑（i5 實際踩過）：
1. npm 版 claude 是 claude.CMD，cmd.exe 解析批次檔參數遇到換行就截斷，後面的 --settings（gate hook）
   整段消失 → 優先用 claude.exe；只找到 .CMD 時解析 shim 實際指向的 js／exe 直接執行；
   長內容一律不上命令列，並由 ensure_single_line_args 把關。
2. --tools 要用 variadic（"--tools", *tools），不要逗號串接。
3. 逾時要殺整棵程序樹（見 proc.py）。
"""

import json
import os
import re
import shlex
import shutil
import sys
import threading
import time
from pathlib import Path

from lhf import fake, integrity, proc
from lhf.common import (  # noqa: F401 — KEEP_CLAUDE_ENV／NOTIFY_SECRET_PREFIX 保留給既有呼叫端
    IN_ENGINE_ENV,
    KEEP_CLAUDE_ENV,
    NOTIFY_SECRET_PREFIX,
    atomic_write_text,
    now_str,
    scrubbed_env,
    stamp,
    strip_nested_env,
)
from lhf.config import ENGINE_DIR, is_fake, posix, render, render_path
from lhf.errors import ConfigError, RoundError
from lhf.state import file_offset, read_jsonl_since, run_path, trace_path

# 只有「找不到指令」才重試：9009＝cmd.exe 找不到、127＝sh 找不到；其他失敗重試只會白燒額度
LAUNCH_FAIL_CODES = {9009, 127}
SHIM_TARGET_RE = re.compile(r'"%~?dp0%?\\?([^"%]+?\.(?:js|exe|cjs|mjs))"', re.IGNORECASE)
HOOK_TIMEOUT_SEC = 30
ALLOW_PROJECT_PERMISSIONS_ENV = "LH_ALLOW_PROJECT_PERMISSIONS"
# 平行站的多個執行緒會同時重寫 hook 設定檔；Windows 上同時寫同一檔可能 PermissionError
HOOK_LOCK = threading.Lock()


# ---------- 找 claude ----------


def find_claude():
    """回傳命令前綴（list）。順序：LH_CLAUDE_BIN → ~/.local/bin/claude.exe → claude.exe → claude（.CMD 解析 shim）。"""
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
        return resolve_shim(Path(found))
    return [found]


def resolve_shim(cmd_path):
    """讀 npm 產生的 .CMD shim，找出它實際執行的 js／exe；不直接執行 .CMD（會截斷多行參數）。"""
    try:
        text = cmd_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RoundError(f"讀不到 {cmd_path}：{exc}") from exc
    for relative in SHIM_TARGET_RE.findall(text):
        target = (cmd_path.parent / relative.replace("\\", "/")).resolve()
        if not target.is_file() or target.name.lower() == "node.exe":  # shim 會先找同目錄的 node.exe 當直譯器
            continue
        if target.suffix.lower() == ".exe":
            return [str(target)]
        local_node = cmd_path.parent / "node.exe"
        node = str(local_node) if local_node.is_file() else shutil.which("node")
        if node:
            return [node, str(target)]
        raise RoundError(f"{cmd_path.name} 指向 {target.name}，但 PATH 上找不到 node")
    raise RoundError(
        f"只找到 {cmd_path}，且看不出它實際執行哪個檔；請安裝原生版 claude.exe "
        "（~/.local/bin/claude.exe）或設 LH_CLAUDE_BIN 指向實際執行檔"
    )


# ---------- 環境與命令列 ----------


def to_msys(path):
    """C:/x/y → /c/x/y；ORIGINAL_PATH 是冒號分隔，Windows 磁碟代號的冒號會把它切壞。"""
    text = posix(path)
    match = re.match(r"^([A-Za-z]):/(.*)$", text)
    return f"/{match.group(1).lower()}/{match.group(2)}" if match else text


def base_env(cfg, rt):
    """所有子程序共用：移除 API key、巢狀 session 變數、所有 LH_*（含執行鎖 token），補上專案 env 與 python 路徑。
    LH_LOCK_TOKEN 絕不外流：拿到它就能沿用 hours 的鎖直接改 state.json，被正在跑的輪覆蓋。"""
    env = os.environ.copy()
    strip_nested_env(env)
    for key in [k for k in env if k.startswith("LH_")]:
        env.pop(key)
    py_dir = Path(sys.executable).parent
    env["PATH"] = str(py_dir) + os.pathsep + env.get("PATH", "")
    if env.get("ORIGINAL_PATH"):
        # Claude 的 Bash 工具用登入 shell 依 ORIGINAL_PATH 重組 PATH，不補就找不到 python
        env["ORIGINAL_PATH"] = to_msys(py_dir) + ":" + env["ORIGINAL_PATH"]
    mapping = cfg.vars(unit=rt["unit"], round_no=rt["round"])
    env.update({key: render(value, mapping) for key, value in (cfg.data.get("env") or {}).items()})
    env["PYTHONUTF8"] = "1"
    env[IN_ENGINE_ENV] = "1"  # 第一道快速檢查：CLI 看到就拒絕人工指令（可被 env -u 清掉，所以另有祖先鏈檢查）
    return env


def command_env(cfg, rt):
    """command 站（跑撰寫者寫的腳本）：不給任何 LH_* 變數，腳本不需要也不該知道引擎的狀態。"""
    return base_env(cfg, rt)


def build_env(cfg, rt, role=None, station_id=None):
    """claude -p 用：只加 gate（claude 的 hook 子程序）判斷所需的 LH_* 變數。"""
    env = base_env(cfg, rt)
    env.update(
        {
            "LH_PROJECT_DIR": str(cfg.project_dir),
            "LH_UNIT": rt["unit"],
            "LH_ROUND": str(rt["round"]),
            "LH_TRACE_OFFSET": str(file_offset(trace_path(cfg))),
        }
    )
    if os.environ.get("LH_WORK_OVERRIDE"):
        env["LH_WORK_OVERRIDE"] = os.environ["LH_WORK_OVERRIDE"]  # gate 要用同一個工作目錄展開 write 路徑
    for key, value in (("LH_ROLE", role), ("LH_STATION", station_id)):
        if value:
            env[key] = value
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
    """長內容一律不上命令列：prompt 走 stdin、角色說明走 --append-system-prompt-file。
    --restricted：不載入任何 CLAUDE.md（含使用者全域 ~/.claude/CLAUDE.md）與 user／project／local 設定檔，
      只有 --tools 指名的工具可用，檔案工具限在工作目錄＋--add-dir；--settings 的 gate hook 照樣生效。
      2026-11-21 以 claude 2.1.329 實測：光靠 --setting-sources project,local 仍會載入全域 CLAUDE.md，
      代理會吃到使用者的個人規則（例如自己 commit＋push），破壞 §9 角色分離。
    --strict-mcp-config：不載入任何 MCP 伺服器。
    --setting-sources project,local：在 --restricted 下已多餘，保留是為了相容舊版 CLI（實測與 --restricted 不衝突）。
    --permission-mode default：不沿用使用者的寬鬆模式（bypass／acceptEdits），一切交給 gate 判斷。"""
    tools = [str(tool) for tool in cfg.roles[role]["tools"]]
    cmd = [
        *prefix,
        "-p",
        "--restricted",
        "--strict-mcp-config",
        "--append-system-prompt-file",
        posix(cfg.agents_dir / f"{role}.md"),
        "--tools",
        *tools,
        "--settings",
        posix(hook_path),
        "--setting-sources",
        "project,local",
        "--permission-mode",
        "default",
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
    entry = {"type": "command", "timeout": HOOK_TIMEOUT_SEC}
    settings = {
        "hooks": {
            "PreToolUse": [{"matcher": "*", "hooks": [{**entry, "command": hook}]}],
            "PostToolUse": [{"matcher": "*", "hooks": [{**entry, "command": hook + " --post"}]}],
        }
    }
    path = run_path(cfg, ".runtime") / "hook-settings.json"
    text = json.dumps(settings, ensure_ascii=False, indent=2)
    with HOOK_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    return path


def risky_project_settings(directory):
    """--setting-sources project,local 會載入工作目錄的 .claude/settings*.json；裡面的 allow／bypass 會放寬權限。"""
    findings = []
    for path in sorted((Path(directory) / ".claude").glob("settings*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            findings.append(f"{path.name}（讀不懂，無法確認）")
            continue
        perms = data.get("permissions") if isinstance(data, dict) else None
        if isinstance(perms, dict) and (
            perms.get("allow") or perms.get("defaultMode") in ("bypassPermissions", "acceptEdits")
        ):
            findings.append(f"{path.name}（permissions.allow／defaultMode）")
    return findings


def check_project_permissions(cfg, cwd):
    if os.environ.get(ALLOW_PROJECT_PERMISSIONS_ENV) == "1":
        return
    findings = []
    for directory in dict.fromkeys([cfg.work_dir(), Path(cwd)]):
        findings += [f"{posix(directory)}/.claude/{item}" for item in risky_project_settings(directory)]
    if findings:
        raise RoundError(
            "工作目錄的 Claude 專案設定放寬了權限："
            + "、".join(findings)
            + "；確認無誤後加 --allow-project-permissions 才能執行"
        )


# ---------- 執行 ----------


def is_launch_failure(result):
    return result.returncode in LAUNCH_FAIL_CODES


def run_with_retry(cfg, call):
    """啟動類失敗（OSError、結束碼 9009／127）等幾秒重試一次；重試不另外扣次數（呼叫前已扣過）。"""
    wait = cfg.limits["launch_retry_wait"]
    for attempt in (1, 2):
        try:
            result = proc.run_tree(
                call["cmd"],
                cwd=call["cwd"],
                env=call["env"],
                timeout=cfg.limits["agent_timeout"],
                input_text=call["prompt"],
            )
        except OSError as exc:
            if attempt == 2:
                raise
            print(f"    （{call['role']} 的 claude 啟動失敗：{exc}，{wait} 秒後重試一次）")
        else:
            if attempt == 2 or not is_launch_failure(result):
                return result
            print(f"    （{call['role']} 的 claude 疑似找不到指令（結束碼 {result.returncode}），{wait} 秒後重試一次）")
        time.sleep(wait)
    raise AssertionError("unreachable")


def prune_fail_logs(cfg):
    logs = sorted(run_path(cfg, "logs").glob("agent-fail-*.txt"))
    for old in logs[: -cfg.limits["fail_logs_max"]]:
        old.unlink(missing_ok=True)


def save_failure_log(cfg, call, stdout, stderr, reason):
    """存完整 stdout／stderr：只截前 300 字的錯誤訊息常常看不出真正原因。"""
    path = run_path(cfg, "logs") / f"agent-fail-{stamp()}-{os.getpid()}-{threading.get_ident()}-{call['role']}.txt"
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
        prune_fail_logs(cfg)
    except OSError as exc:
        return f"（寫入失敗紀錄 {path.name} 失敗：{exc}）"
    return posix(path)


def invoke(cfg, call):
    try:
        result = run_with_retry(cfg, call)
    except OSError as exc:
        raise RoundError(f"{call['role']} 的 claude -p 無法啟動：{exc}") from exc
    if result.timed_out:
        log = save_failure_log(cfg, call, result.stdout, result.stderr, "逾時")
        raise RoundError(
            f"{call['role']} 的 claude -p 逾時（超過 {cfg.limits['agent_timeout']} 秒，已結束整棵程序樹；"
            f"完整輸出：{log}）"
        )
    return result


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
    check_project_permissions(cfg, cwd)
    cmd = build_claude_cmd(cfg, role, find_claude(), write_hook_settings(cfg), mapping)
    return {"role": role, "cmd": cmd, "prompt": prompt, "cwd": str(cwd), "env": build_env(cfg, rt, role, station_id)}


def call_agent(cfg, rt, station, job):
    """所有 claude -p 呼叫集中在這裡。job＝{role, prompt_text, outputs}；回傳 result／session_id／cost／turns。"""
    role = job["role"]
    if is_fake():
        return fake.fake_agent(cfg, rt, station, job)  # 假模式不跑 hook，所以也不做 gate 生效檢查
    call = prepare_call(cfg, rt, role, job["prompt_text"], station["id"])
    offset = file_offset(trace_path(cfg))  # 呼叫前記下位置，之後只看這次呼叫新增的 trace
    result = invoke(cfg, call)
    try:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:300]
            raise RoundError(f"{role} 的 claude -p 結束碼 {result.returncode}：{detail}")
        info = parse_agent_json(role, result.stdout)
    except RoundError as exc:
        log = save_failure_log(cfg, call, result.stdout, result.stderr, str(exc))
        raise RoundError(f"{exc}（完整輸出：{log}）") from exc
    verify_gate_active(cfg, role, rt["unit"], offset)
    return info


def describe_cmd(cfg, rt, role):
    """dry-run 用：不找真的 claude、不寫 hook 檔，只組出長相一樣的命令列。"""
    mapping = cfg.vars(unit=rt["unit"], round_no=rt["round"])
    hook = run_path(cfg, ".runtime") / "hook-settings.json"
    return build_claude_cmd(cfg, role, ["claude"], hook, mapping)


# ---------- 啟動前檢查：claude 支援 --restricted ----------

CAPABILITY_NAME = integrity.CAPABILITY_NAME
HELP_TIMEOUT_SEC = 60
UPGRADE_HINT = "請升級 claude（例：claude update，或重新安裝原生版 ~/.local/bin/claude.exe）"


def binary_identity(prefix):
    """命令前綴裡每個檔案的路徑＋大小＋修改時間；升級後一定不同，快取自然失效。"""
    identity = []
    for part in prefix:
        path = Path(part)
        try:
            info = path.stat()
        except OSError:
            identity.append([str(part), None, None])
            continue
        identity.append([posix(path.resolve()), info.st_size, info.st_mtime_ns])
    return identity


def capability_path(cfg):
    return run_path(cfg, ".runtime") / CAPABILITY_NAME


def cached_ok(cfg, identity):
    try:
        record = json.loads(capability_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("identity") == identity and record.get("restricted") is True


def help_env():
    return scrubbed_env()


def read_help(prefix):
    try:
        result = proc.run_tree([*prefix, "--help"], cwd=str(ENGINE_DIR), env=help_env(), timeout=HELP_TIMEOUT_SEC)
    except OSError as exc:
        raise ConfigError(f"無法執行 claude --help：{exc}；{UPGRADE_HINT}") from exc
    if result.timed_out:
        raise ConfigError(f"claude --help 超過 {HELP_TIMEOUT_SEC} 秒沒有回應；{UPGRADE_HINT}")
    return result.stdout + result.stderr


def ensure_restricted_support(cfg):
    """run／hours 啟動時呼叫：claude 沒有 --restricted 就拒絕啟動（ConfigError → 結束碼 2）。
    不支援時代理會載入使用者全域 CLAUDE.md（§11 實測），破壞角色分離，不能「先跑再說」。
    結果快取在 run/.runtime/claude-capability.json（以執行檔路徑＋大小＋修改時間為鍵），每輪不重跑。
    假模型模式不呼叫 claude，直接略過。"""
    if is_fake():
        return "fake"
    try:
        prefix = find_claude()
    except RoundError as exc:
        raise ConfigError(f"{exc}；{UPGRADE_HINT}") from exc
    identity = binary_identity(prefix)
    if cached_ok(cfg, identity):
        return "cached"
    if "--restricted" not in read_help(prefix):
        raise ConfigError(f"claude（{' '.join(prefix)}）的 --help 沒有 --restricted；{UPGRADE_HINT}")
    record = {"identity": identity, "restricted": True, "checked": now_str()}
    try:
        atomic_write_text(capability_path(cfg), json.dumps(record, ensure_ascii=False))
    except OSError as exc:
        print(f"（警告：寫不進 {CAPABILITY_NAME}：{exc}；下次會再檢查一次）", file=sys.stderr)
    return "checked"
