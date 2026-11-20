"""畢業專案「K143 演練備課工廠」主控程式：一次執行＝一輪＝一章，固定 6 站。

站：1 plan（planner）→ 2a write（writer）∥ 2b now（now-updater）→ 3 run（本程式自己跑 bash）
    → 4 judge（tester）→ 5 review（reviewer）→ 6 commit（本程式在 WORKTREE git commit，不 push）
每站做完立刻原子寫入 state.json；中斷後再執行，會從該章未完成的那一站接續。
介面約定見同目錄 CONTRACT.md。

用法（Git Bash，記得 PYTHONUTF8=1）：
  python run_round.py                      取 queue 中第一個未結束的章跑一輪
  python run_round.py --chapter ch04       指定章
  python run_round.py --init ch04 ch05     建立／重設 queue（加 --force 才重設已有章節狀態）
  python run_round.py --status             印出狀態，不呼叫模型
  python run_round.py --dry-run            印出每站要執行的指令與 prompt 摘要，不執行

測試鉤子（只給冒煙測試用）：
  LH_FAKE_MODEL=1                 不呼叫 claude，假函式依角色產生檔案；站 6 不 commit
  LH_FAKE_VERDICT=FAIL_ONCE       假 tester 每章第一次判 FAIL、之後 PASS
  LH_FAKE_INTERRUPT_AFTER=<站名>  該站完成並存檔後 raise KeyboardInterrupt（模擬 Ctrl+C）
  LH_PRACTICE_OVERRIDE=<路徑>     改用別的 PRACTICE 目錄（假模式預設 capstone/.fake-practice）
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:  # 沒裝 pyyaml 時退回內建工具清單，不讓整個工廠停擺
    yaml = None

CAPSTONE =Path(__file__).resolve().parent
WORKTREE = Path.home() / "workspace" / "kindle-143-fable5-agentic-ai-factory"
NOW_TABLE = Path.home() / "workspace" / "console" / "drills" / "k143-interactive" / "fable-5-to-5.2.md"
STATE_PATH = CAPSTONE / "state.json"
HEARTBEAT_PATH = CAPSTONE / "heartbeat.txt"
POLICY_PATH = CAPSTONE / "policy.yaml"
AGENTS_DIR = CAPSTONE / "agents"
RUNTIME_DIR = CAPSTONE / ".runtime"
FAKE_PRACTICE = CAPSTONE / ".fake-practice"

FINAL_STATUSES = {"done", "failed", "rejected"}
STEP_COUNT = 8
AGENT_TIMEOUT_SEC = 900
STEP_TIMEOUT_SEC = 180
MAX_CALLS_PER_ROUND = 10
MAX_REWRITES = 2
MAX_REVIEW_RETRIES = 1
# 只為了讓摘要好讀的概略匯率；實際走 Max 訂閱不扣錢，這是「等值成本」
USD_TO_TWD = 32.0
FEEDBACK_MAX_CHARS = 6000
CLAUDE_RETRY_WAIT_SEC = 5
LAUNCH_FAIL_HINTS = ("not found", "not recognized")
CHAPTER_RE = re.compile(r"^ch\d{2}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]")

# policy.yaml 不在或讀不到時的備援，內容與 CONTRACT／policy.yaml 一致
DEFAULT_TOOLS = {
    "planner": ["Read", "Glob", "Grep", "Write"],
    "writer": ["Read", "Glob", "Grep", "Write", "Edit"],
    "now-updater": ["Read", "Grep", "Write"],
    "tester": ["Read", "Write"],
    "reviewer": ["Read", "Glob", "Grep", "Write"],
}
# 巢狀 session 變數會讓子 claude 以為自己在別的 session 裡；以下幾個是正常設定要保留
KEEP_CLAUDE_ENV = {"CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}

COMMON_RULES = (
    "\n\n規則：全程使用繁體中文（程式碼本體除外）。絕對不可修改、新增或刪除 practice/fable5/ 底下任何檔案。"
    "不要執行 git，也不要執行任何 shell 指令。只寫入本任務指定的檔案。完成後用一兩句話回報寫了哪些檔案。"
)


class RoundError(Exception):
    """可預期的本輪錯誤（模型呼叫失敗、檔案缺漏等）；章節維持原站，下次執行重試該站。"""


class CallBudgetExceeded(RoundError):
    """本輪 claude -p 次數超過上限；章節直接標 failed，避免無止境重試燒額度。"""


# ---------- 路徑 ----------

def is_fake():
    return os.environ.get("LH_FAKE_MODEL") == "1"


def practice_dir():
    """假模式一定不碰真的 worktree：沒指定 override 就用 capstone/.fake-practice。"""
    override = os.environ.get("LH_PRACTICE_OVERRIDE")
    if override:
        return Path(override).resolve()
    if is_fake():
        return FAKE_PRACTICE
    return WORKTREE / "practice"


def chapter_paths(ch):
    """一章用到的所有檔案集中在這裡，各站與 prompt 都從這裡取，避免路徑不一致。"""
    interactive = practice_dir() / "interactive"
    return {
        "practice": practice_dir(),
        "interactive": interactive,
        "prompt_md": WORKTREE / "prompts" / f"{ch}.md",
        "plan": CAPSTONE / "plans" / f"{ch}.md",
        "script": interactive / f"{ch}.sh",
        "coach": interactive / f"coach-{ch}.txt",
        "now": interactive / f"now-{ch}.txt",
        "raw": CAPSTONE / "test-logs" / f"{ch}-raw.log",
        "testlog": CAPSTONE / "test-logs" / f"{ch}.md",
        "review": CAPSTONE / "reviews" / f"{ch}.md",
    }


def posix(path):
    return Path(path).as_posix()


def chapter_no(ch):
    return int(ch[2:])


# ---------- 狀態存取 ----------

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_state():
    return {"round": 0, "queue": [], "chapters": {}, "history": []}


def new_chapter():
    """CONTRACT 欄位之外多存 round／now_done／feedback／cost，接續與重寫都要用到。"""
    return {"status": "pending", "rewrites": 0, "review_retries": 0, "verdicts": [], "calls": 0,
            "started": None, "finished": None, "round": None, "now_done": False,
            "feedback": None, "cost_usd": 0.0, "sessions": []}


def load_state():
    """壞檔直接報錯，不默默重建，以免蓋掉多輪累積的進度。"""
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoundError(f"state.json 讀取失敗（{STATE_PATH}）：{exc}") from exc


def save_state(state):
    """先寫 .tmp 再 os.replace：replace 是原子操作，中斷時不會留下寫一半的 state.json。"""
    tmp_path = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, STATE_PATH)


def log_event(state, ch, event):
    """記一筆 history 並立刻存檔——這就是 checkpoint。"""
    round_no = state["chapters"].get(ch, {}).get("round") or state["round"]
    state["history"].append({"ts": now_str(), "round": round_no, "chapter": ch, "event": event})
    save_state(state)
    print(f"    · {event}")


# ---------- claude -p 呼叫 ----------

def load_role_tools(role):
    """工具清單以 policy.yaml 為準（gate.py 也讀同一份）；讀不到才退回內建預設。"""
    if yaml is None:
        print("    （沒有安裝 pyyaml，工具清單改用內建預設）")
        return DEFAULT_TOOLS[role]
    try:
        policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
        tools = policy["roles"][role]["tools"]
        if isinstance(tools, list) and tools:
            return [str(tool) for tool in tools]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        print(f"    （policy.yaml 讀不到 {role} 的工具：{exc}，改用內建預設）")
    return DEFAULT_TOOLS[role]


def read_agent_prompt(role):
    path = AGENTS_DIR / f"{role}.md"
    if not path.exists():
        raise RoundError(f"找不到角色說明 {path}（主控者要先寫好 agents/{role}.md）")
    return path.read_text(encoding="utf-8")


def write_hook_settings():
    """每次執行都重寫，確保 python 與 gate.py 路徑跟著這台電腦走。"""
    py = posix(sys.executable)
    gate = posix(CAPSTONE / "gate.py")
    hook = f'"{py}" "{gate}"'
    settings = {"hooks": {
        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": hook}]}],
        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": hook + " --post"}]}],
    }}
    RUNTIME_DIR.mkdir(exist_ok=True)
    path = RUNTIME_DIR / "hook-settings.json"
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def to_msys(path):
    """C:/x/y → /c/x/y；ORIGINAL_PATH 是冒號分隔，Windows 磁碟代號的冒號會把它切壞。"""
    text = posix(path)
    match = re.match(r"^([A-Za-z]):/(.*)$", text)
    return f"/{match.group(1).lower()}/{match.group(2)}" if match else text


def build_env(ctx, role=None):
    """移除 API key（成本鐵律）與巢狀 session 變數，補上角色與 python 路徑。"""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    for key in list(env):
        if key in KEEP_CLAUDE_ENV:
            continue
        if key == "CLAUDECODE" or key == "CLAUDE_PID" or key.startswith("CLAUDE_CODE_"):
            env.pop(key)
    py_dir = Path(sys.executable).parent
    env["PATH"] = str(py_dir) + os.pathsep + env.get("PATH", "")
    if env.get("ORIGINAL_PATH"):
        # Claude 的 Bash 工具用登入 shell 依 ORIGINAL_PATH 重組 PATH，不補就找不到 python
        env["ORIGINAL_PATH"] = to_msys(py_dir) + ":" + env["ORIGINAL_PATH"]
    env.update({"LH_CHAPTER": ctx["chapter"], "LH_ROUND": str(ctx["round"]),
                "PYTHONUTF8": "1", "K143_CLI_MAX_CALLS": "6"})
    if role:
        env["LH_ROLE"] = role
    return env


def build_claude_cmd(role, claude_path, hook_path, system_prompt):
    """參數照 CONTRACT；prompt 改走 stdin，避免 Windows 命令列 32K 字元上限。"""
    return [claude_path, "-p",
            "--append-system-prompt", system_prompt,
            "--tools", ",".join(load_role_tools(role)),
            "--settings", posix(hook_path),
            "--setting-sources", "project,local",
            "--add-dir", posix(CAPSTONE),
            "--add-dir", posix(NOW_TABLE.parent),
            # prompts/ 在 WORKTREE 根目錄、不在 cwd(PRACTICE) 底下，planner／reviewer 要讀
            "--add-dir", posix(WORKTREE / "prompts"),
            "--output-format", "json"]


def is_launch_failure(proc):
    """結束碼非 0 且 stderr 像「找不到指令」，才算啟動類失敗；其他錯誤不重試。"""
    stderr = (proc.stderr or "").lower()
    return proc.returncode != 0 and any(hint in stderr for hint in LAUNCH_FAIL_HINTS)


def run_claude_with_retry(role, cmd, prompt, env):
    """i5 的 npm 版 claude 第一次在管線呼叫曾偶發 command not found：啟動類失敗等 5 秒重試一次。
    重試不另外扣 claude -p 次數（reserve_calls 已在呼叫前扣過一次）。"""
    for attempt in (1, 2):
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=AGENT_TIMEOUT_SEC, env=env,
                                  cwd=practice_dir(), check=False)
        except OSError as exc:
            if attempt == 2:
                raise
            print(f"    （{role} 的 claude 啟動失敗：{exc}，{CLAUDE_RETRY_WAIT_SEC} 秒後重試一次）")
        else:
            if attempt == 2 or not is_launch_failure(proc):
                return proc
            print(f"    （{role} 的 claude 疑似找不到指令（結束碼 {proc.returncode}），"
                  f"{CLAUDE_RETRY_WAIT_SEC} 秒後重試一次）")
        time.sleep(CLAUDE_RETRY_WAIT_SEC)
    raise AssertionError("unreachable")


def call_agent(role, prompt, ctx):
    """所有 claude -p 呼叫集中在這裡。回傳 dict：result、session_id、cost、turns。"""
    if is_fake():
        return fake_agent(role, ctx)
    claude_path = shutil.which("claude")
    if claude_path is None:
        raise RoundError("找不到 claude 執行檔（shutil.which('claude') 為 None）")
    cmd = build_claude_cmd(role, claude_path, write_hook_settings(), read_agent_prompt(role))
    try:
        proc = run_claude_with_retry(role, cmd, prompt, build_env(ctx, role))
    except subprocess.TimeoutExpired as exc:
        raise RoundError(f"{role} 的 claude -p 逾時（超過 {AGENT_TIMEOUT_SEC} 秒）") from exc
    except OSError as exc:
        raise RoundError(f"{role} 的 claude -p 無法啟動：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise RoundError(f"{role} 的 claude -p 結束碼 {proc.returncode}：{detail}")
    return parse_agent_json(role, proc.stdout)


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
    return {"result": payload["result"], "session_id": payload.get("session_id", ""),
            "cost": float(payload.get("total_cost_usd") or 0.0), "turns": payload.get("num_turns")}


def reserve_calls(state, ch, count):
    """呼叫前先扣次數並存檔：就算呼叫中途當掉也算一次，防止反覆重試無上限。"""
    rec = state["chapters"][ch]
    if rec["calls"] + count > MAX_CALLS_PER_ROUND:
        raise CallBudgetExceeded(
            f"本輪 claude -p 已用 {rec['calls']} 次，再呼叫 {count} 次會超過上限 {MAX_CALLS_PER_ROUND}")
    rec["calls"] += count
    save_state(state)


def record_call(state, ch, role, info):
    rec = state["chapters"][ch]
    rec["cost_usd"] = round(rec["cost_usd"] + info["cost"], 6)
    rec["sessions"].append({"role": role, "session_id": info["session_id"],
                            "cost_usd": info["cost"], "turns": info["turns"]})
    print(f"    ✔ {role} 完成（{info['turns']} 輪對話、US$ {info['cost']:.4f}）")


# ---------- 假模型（LH_FAKE_MODEL=1） ----------

def fake_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def fake_plan(ch, paths):
    steps = "\n".join(f"## 第 {i} 步：假步驟 {i}\n- 目的：測試\n- 指令：echo\n- 預期：印出一行\n- 呼叫模型：否\n"
                      for i in range(1, STEP_COUNT + 1))
    fake_write(paths["plan"], f"# {ch} 假規劃\n\n{steps}")


def fake_writer(ch, paths):
    """最小可跑腳本：8 步每步 echo 一行，讓站 3 有真的 bash 可以跑。"""
    cases = "\n".join(f'{i}) echo "{ch} 第 {i} 步（假）" ;;' for i in range(1, STEP_COUNT + 1))
    script = f'#!/bin/bash\n# {ch} 假腳本\nset -u\nSTEP="${{1:-1}}"\ncase "$STEP" in\n{cases}\n*) echo "步驟要 1～8" ;;\nesac\n'
    fake_write(paths["script"], script)
    coach = "\n\n".join(f"【第 {i} 步：假步驟 {i}】\n這是假教練稿。目的，是測試。" for i in range(1, STEP_COUNT + 1))
    fake_write(paths["coach"], f"K143 第 {chapter_no(ch)} 章互動演練（假教練稿）\n\n{coach}\n")


def fake_now(ch, paths):
    fake_write(paths["now"], f"K143 第 {chapter_no(ch)} 章「🆕 現況」補充\n本章沒有 Fable 5.2 差異\n")


def fake_tester(ch, paths):
    verdict = "PASS"
    if os.environ.get("LH_FAKE_VERDICT") == "FAIL_ONCE":
        # 用標記檔記住「已經 FAIL 過」，跨程序（中斷接續）也成立
        marker = RUNTIME_DIR / f"fake-fail-once-{ch}"
        if not marker.exists():
            fake_write(marker, "x")
            verdict = "FAIL"
    fake_write(paths["testlog"], f"# {ch} 假測試判定\n\n第 3 步輸出不符（假）\n\nVERDICT: {verdict}\n")


def fake_reviewer(ch, paths):
    fake_write(paths["review"], f"# {ch} 假審查\n\n全部 OK（假）\n\nVERDICT: PASS\n")


FAKE_ROLES = {"planner": fake_plan, "writer": fake_writer, "now-updater": fake_now,
              "tester": fake_tester, "reviewer": fake_reviewer}


def fake_agent(role, ctx):
    ch = ctx["chapter"]
    FAKE_ROLES[role](ch, chapter_paths(ch))
    stamp = datetime.now().strftime("%H%M%S%f")
    return {"result": f"（假）{role} 完成", "session_id": f"fake-{role}-{stamp}", "cost": 0.0, "turns": 1}


# ---------- 各站 prompt ----------

def prompt_plan(ch, paths):
    inter = posix(paths["interactive"])
    return (
        f"你是 K143《Fable 5》互動演練的規劃者，負責第 {chapter_no(ch)} 章（{ch}）。\n"
        f"請先閱讀：本章提示詞 {posix(paths['prompt_md'])}；書附範例 {posix(paths['practice'])}/fable5/"
        f"（用 Glob／Grep 找出本章相關模組與 examples，確認檔案真的存在）；格式範本 {inter}/ch01.sh、{inter}/ch03.sh。\n"
        f"然後把規劃寫成 Markdown 存到 {posix(paths['plan'])}。\n"
        f"規劃剛好 {STEP_COUNT} 步，每步都要有五項：\n"
        "1. 標題（一行）\n2. 目的：白話說明這一步要讓學習者懂什麼\n"
        "3. 要跑的指令或內嵌 Python（完整可直接執行；cwd 是 practice/fable5，用相對路徑；只能用書附真的存在的模組與檔案）\n"
        "4. 預期看到什麼（具體輸出特徵，讓測試者能判斷對錯）\n5. 是否呼叫模型（是／否）\n"
        "限制：最多 2 步呼叫模型（透過書附 claude_cli_client 轉接層走 claude -p），其他步一律離線；"
        f"不需要互動輸入；要動手改或產出的檔案寫到 practice/{ch}/interactive/，不碰 fable5/；第 8 步是本章總結。"
        + COMMON_RULES)


def feedback_text(rec):
    """重寫時把上一版沒過的原因整段附上，寫手才知道要修什麼。"""
    feedback = rec.get("feedback")
    if not feedback:
        return ""
    path = Path(feedback["path"])
    kind = "測試" if feedback["kind"] == "test" else "審查"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")[-FEEDBACK_MAX_CHARS:]
    except OSError as exc:
        content = f"（讀不到 {path}：{exc}）"
    return (f"\n\n這是重寫。上一版沒有通過{kind}，原因如下（來自 {posix(path)}）：\n-----\n{content}\n-----\n"
            "請針對以上問題修正兩個檔案（整檔覆寫），沒問題的步驟保持不變。")


def prompt_write(ch, paths, rec):
    inter = posix(paths["interactive"])
    no = chapter_no(ch)
    return (
        f"你是 K143 第 {no} 章互動演練的寫手。依規劃 {posix(paths['plan'])} 寫兩個檔案：\n"
        f"1. {posix(paths['script'])}：bash 腳本，格式完全照 {inter}/ch01.sh 與 {inter}/ch03.sh——開頭註解（章名、"
        f"用法 bash {ch}.sh <步驟 1-8>、是否呼叫模型）、set -u、STEP／HERE／PRACTICE／LAB／WORK 變數、"
        "相同的 banner／tip／show／done_step／sh_run／py_run 函式、case 1)～8) 加 *) 預設分支；"
        f"banner 寫「K143 第 {no} 章｜<短章名>」。有步驟呼叫模型就照 ch03.sh 加 export K143_CLI_MAX_CALLS=6。"
        f"WORK 指到 practice/{ch}/interactive。每步只執行規劃中的指令，不可讀互動輸入。\n"
        f"2. {posix(paths['coach'])}：教練稿。第一行「K143 第 {no} 章互動演練：<章名>（教練稿）」，"
        "空一行後每步一段：「【第 N 步：標題】」換行接 3～5 句白話教練稿（口語、念得出來，說明要做什麼、會看到什麼），"
        "每段最後一句以「目的，是…」開頭；段與段之間空一行。不要寫「🆕 現況」內容（那由別人寫）。"
        + feedback_text(rec) + COMMON_RULES)


def now_table_row(ch):
    """抓對照表中本章那一列，直接放進 prompt，減少模型讀錯列的機會。"""
    try:
        text = NOW_TABLE.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf"^\|\s*{ch}\s*\|.*$", text, re.MULTILINE)
    return match.group(0) if match else None


def prompt_now(ch, paths):
    no = chapter_no(ch)
    row = now_table_row(ch) or "（對照表沒有本章這一列＝本章沒有差異）"
    return (
        f"你負責 K143 第 {no} 章的「🆕 現況」補充檔 {posix(paths['now'])}。\n"
        f"格式範本：{posix(paths['interactive'])}/now-ch02.txt。完整對照表：{posix(NOW_TABLE)}。"
        f"本章提示詞：{posix(paths['prompt_md'])}。規劃：{posix(paths['plan'])}（看每步在做什麼，決定現況掛在哪一步）。\n"
        f"對照表中本章那一列：{row}\n"
        f"有差異時：第一行「K143 第 {no} 章「🆕 現況」補充（書是 Fable 5，2026-11-09 已推出 Fable 5.2）」，"
        "第二行照範本寫說明行，空一行後每個相關步驟一段「【第 N 步 🆕 現況】」換行接 2～4 句白話說明；"
        "只寫對照表有根據的內容，不可自己編新功能。\n"
        f"沒有差異時：只寫兩行——同樣的標題、「本章沒有 Fable 5.2 差異」。" + COMMON_RULES)


def prompt_judge(ch, paths):
    return (
        f"你是 K143 第 {chapter_no(ch)} 章互動演練的測試判定者。讀規劃 {posix(paths['plan'])} 與實際執行紀錄 "
        f"{posix(paths['raw'])}（含 bash -n 結果、bash {ch}.sh 1..8 每步的 exit code、耗時、輸出）。\n"
        f"逐步對照規劃的「預期看到什麼」判斷每步 PASS 或 FAIL，寫到 {posix(paths['testlog'])}：\n"
        "- 一張表：步驟｜標題｜exit code｜判定｜理由（一句話）\n"
        "- FAIL 的步驟寫具體原因與建議怎麼修（給寫手看）\n"
        "- 判定原則：exit code 非 0、逾時、出現 Traceback／command not found／No such file、輸出明顯與預期不符 → FAIL；"
        "數字或措辭的小差異不算 FAIL\n"
        "- 檔案最後一行必須單獨是 `VERDICT: PASS`（8 步全過）或 `VERDICT: FAIL`" + COMMON_RULES)


def prompt_review(ch, paths):
    inter = posix(paths["interactive"])
    return (
        f"你是 K143 第 {chapter_no(ch)} 章互動演練的審查者（做的人不當審的人）。審查成品：腳本 {posix(paths['script'])}、"
        f"教練稿 {posix(paths['coach'])}、現況 {posix(paths['now'])}；參考規劃 {posix(paths['plan'])}、"
        f"測試紀錄 {posix(paths['testlog'])}、本章提示詞 {posix(paths['prompt_md'])}、範本 {inter}/ch01.sh 與 {inter}/coach-ch01.txt。\n"
        "審查項目：1. 8 步是否涵蓋本章重點、順序合理；2. 腳本格式與範本一致、不寫入 fable5/、不需互動輸入；"
        "3. 教練稿每步 3～5 句白話、結尾「目的，是…」、與腳本步驟一一對應；4. 現況檔只寫對照表有根據的內容；"
        "5. 呼叫模型的步驟不超過 2 步。\n"
        f"寫到 {posix(paths['review'])}：每項 OK 或問題、必改清單（若有）。"
        "最後一行必須單獨是 `VERDICT: PASS` 或 `VERDICT: REJECT`（有任何必改就 REJECT）。" + COMMON_RULES)


# ---------- 判決 ----------

def read_verdict(path, allowed):
    """讀檔案最後一個非空行的 VERDICT；缺檔或格式不對一律當不通過（fail closed）。"""
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return allowed[1], f"找不到判定檔 {path.name}"
    match = re.fullmatch(r"`?VERDICT:\s*([A-Z]+)`?", lines[-1]) if lines else None
    if match and match.group(1) in allowed:
        return match.group(1), ""
    return allowed[1], f"{path.name} 最後一行不是合法 VERDICT"


def apply_judge_verdict(state, ch, verdict, note=""):
    """FAIL 回寫手（最多重寫 2 次），還是 FAIL 就標 failed。"""
    rec = state["chapters"][ch]
    rec["verdicts"].append({"ts": now_str(), "station": "judge", "verdict": verdict, "note": note})
    if verdict == "PASS":
        rec["status"] = "judged"
        log_event(state, ch, "測試判定 PASS")
    elif rec["rewrites"] >= MAX_REWRITES:
        finish_chapter(state, ch, "failed", f"測試 FAIL，已重寫 {MAX_REWRITES} 次仍失敗 {note}".strip())
    else:
        rec["rewrites"] += 1
        rec["feedback"] = {"kind": "test", "path": str(chapter_paths(ch)["testlog"])}
        rec["status"] = "planned"
        log_event(state, ch, f"測試判定 FAIL {note}→ 回寫手第 {rec['rewrites']} 次重寫".replace("  ", " "))
    return verdict


def apply_review_verdict(state, ch, verdict, note=""):
    """REJECT 回寫手一次，再 REJECT 就標 rejected。"""
    rec = state["chapters"][ch]
    rec["verdicts"].append({"ts": now_str(), "station": "review", "verdict": verdict, "note": note})
    if verdict == "PASS":
        rec["status"] = "reviewed"
        log_event(state, ch, "審查 PASS")
    elif rec["review_retries"] >= MAX_REVIEW_RETRIES:
        finish_chapter(state, ch, "rejected", f"審查 REJECT，已退回重做 {MAX_REVIEW_RETRIES} 次仍不過 {note}".strip())
    else:
        rec["review_retries"] += 1
        rec["feedback"] = {"kind": "review", "path": str(chapter_paths(ch)["review"])}
        rec["status"] = "planned"
        log_event(state, ch, f"審查 REJECT {note}→ 帶審查意見回寫手".replace("  ", " "))
    return verdict


def finish_chapter(state, ch, status, event):
    rec = state["chapters"][ch]
    rec["status"] = status
    rec["finished"] = now_str()
    log_event(state, ch, event)


# ---------- 六站 ----------

def station_plan(state, ch, ctx):
    paths = chapter_paths(ch)
    if not is_fake() and not paths["prompt_md"].exists():
        raise RoundError(f"找不到本章提示詞 {paths['prompt_md']}")
    paths["plan"].parent.mkdir(parents=True, exist_ok=True)
    reserve_calls(state, ch, 1)
    record_call(state, ch, "planner", call_agent("planner", prompt_plan(ch, paths), ctx))
    if not paths["plan"].exists():
        raise RoundError(f"planner 沒有寫出規劃 {paths['plan']}")
    state["chapters"][ch]["status"] = "planned"
    log_event(state, ch, f"規劃完成 → {paths['plan'].name}")
    return "規劃完成"


def run_parallel(jobs, ctx):
    """2a、2b 平行跑；兩個都等完才回，成功的結果不因另一個失敗而丟掉。"""
    results, errors = {}, {}
    pool = ThreadPoolExecutor(max_workers=len(jobs))
    try:
        futures = {role: pool.submit(call_agent, role, prompt, ctx) for role, prompt in jobs}
        for role, future in futures.items():
            try:
                results[role] = future.result()
            except RoundError as exc:
                errors[role] = str(exc)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results, errors


def station_write(state, ch, ctx):
    """寫手每次都跑；現況檔只需寫一次，重寫輪不再重做以省次數。"""
    rec, paths = state["chapters"][ch], chapter_paths(ch)
    paths["interactive"].mkdir(parents=True, exist_ok=True)
    jobs = [("writer", prompt_write(ch, paths, rec))]
    if not rec["now_done"]:
        jobs.append(("now-updater", prompt_now(ch, paths)))
    reserve_calls(state, ch, len(jobs))
    results, errors = run_parallel(jobs, ctx)
    for role, info in results.items():
        record_call(state, ch, role, info)
    if "now-updater" in results and paths["now"].exists():
        rec["now_done"] = True
    elif "now-updater" in results:
        errors["now-updater"] = f"沒有寫出 {paths['now'].name}"
    if errors:
        save_state(state)
        raise RoundError("站 2 失敗：" + "；".join(f"{role}：{msg}" for role, msg in errors.items()))
    rec["feedback"] = None
    rec["status"] = "written"
    log_event(state, ch, f"寫手完成（{'、'.join(results)}）")
    return "＋".join(results)


def run_one(cmd, cwd, env):
    """跑一個指令，回傳 (exit code 或「逾時」, 秒數, 去掉 ANSI 色碼的輸出)。"""
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=STEP_TIMEOUT_SEC, stdin=subprocess.DEVNULL, check=False)
        code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or b""
        code = "逾時"
        output = partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial
    except OSError as exc:
        code, output = "無法執行", str(exc)
    return code, time.monotonic() - start, ANSI_RE.sub("", output)


def format_block(title, cmd, code, elapsed, output):
    return (f"===== {title} =====\n指令：{shlex.join(cmd)}\nexit code：{code}　耗時：{elapsed:.1f} 秒\n"
            f"----- 輸出 -----\n{output.rstrip()}\n")


def run_script_checks(paths, env, bash):
    """先 bash -n 再 1..8 各跑一次；回傳 (raw log 文字, 自動 FAIL 原因或空字串)。"""
    cwd, script = paths["interactive"], paths["script"].name
    missing = [p.name for p in (paths["script"], paths["coach"]) if not p.exists()]
    if missing:
        return f"缺檔：{'、'.join(missing)}\n", f"寫手沒有產出 {'、'.join(missing)}"
    code, elapsed, output = run_one([bash, "-n", script], cwd, env)
    blocks = [format_block("bash -n 語法檢查", [bash, "-n", script], code, elapsed, output)]
    if code != 0:
        return "\n".join(blocks), f"bash -n 語法檢查失敗：{output.strip()[:300]}"
    for step in range(1, STEP_COUNT + 1):
        print(f"    執行第 {step} 步…")
        code, elapsed, output = run_one([bash, script, str(step)], cwd, env)
        blocks.append(format_block(f"第 {step} 步", [bash, script, str(step)], code, elapsed, output))
    return "\n".join(blocks), ""


def station_run(state, ch, ctx):
    """不呼叫模型；腳本缺檔或語法錯直接當 FAIL，省一次 tester 呼叫。"""
    paths = chapter_paths(ch)
    bash = shutil.which("bash")
    if bash is None:
        raise RoundError("找不到 bash 執行檔")
    raw_text, auto_fail = run_script_checks(paths, build_env(ctx), bash)
    paths["raw"].parent.mkdir(parents=True, exist_ok=True)
    header = f"# {ch} 執行紀錄　{now_str()}　bash={bash}\n\n"
    paths["raw"].write_text(header + raw_text, encoding="utf-8")
    if auto_fail:
        paths["testlog"].write_text(f"# {ch} 自動判定（站 3）\n\n{auto_fail}\n\n原始紀錄：{posix(paths['raw'])}\n\n"
                                    "VERDICT: FAIL\n", encoding="utf-8")
        apply_judge_verdict(state, ch, "FAIL", "（站 3 自動）")
        return "FAIL（自動：" + auto_fail[:40] + "）"
    state["chapters"][ch]["status"] = "ran"
    log_event(state, ch, f"腳本執行完成 → {paths['raw'].name}")
    return "執行完成"


def station_judge(state, ch, ctx):
    paths = chapter_paths(ch)
    paths["testlog"].unlink(missing_ok=True)  # 刪舊判定，避免 tester 沒寫檔卻讀到上一版的 PASS
    reserve_calls(state, ch, 1)
    record_call(state, ch, "tester", call_agent("tester", prompt_judge(ch, paths), ctx))
    verdict, note = read_verdict(paths["testlog"], ("PASS", "FAIL"))
    return apply_judge_verdict(state, ch, verdict, note)


def station_review(state, ch, ctx):
    paths = chapter_paths(ch)
    paths["review"].parent.mkdir(parents=True, exist_ok=True)
    paths["review"].unlink(missing_ok=True)
    reserve_calls(state, ch, 1)
    record_call(state, ch, "reviewer", call_agent("reviewer", prompt_review(ch, paths), ctx))
    verdict, note = read_verdict(paths["review"], ("PASS", "REJECT"))
    return apply_review_verdict(state, ch, verdict, note)


def git(args):
    try:
        return subprocess.run(["git", "-C", str(WORKTREE), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False)
    except OSError as exc:
        raise RoundError(f"無法執行 git：{exc}") from exc


def commit_files(ch):
    return [f"practice/interactive/{name}" for name in (f"{ch}.sh", f"coach-{ch}.txt", f"now-{ch}.txt")]


def station_commit(state, ch, ctx):
    """只 commit 本章三個檔（git commit -- <paths>），不 push；假模式或改過 PRACTICE 一律不碰真的 worktree。"""
    if is_fake() or os.environ.get("LH_PRACTICE_OVERRIDE"):
        print("    （假模式）略過 commit")
        finish_chapter(state, ch, "done", "完成（假模式略過 commit）")
        return "略過"
    files = commit_files(ch)
    added = git(["add", "--", *files])
    if added.returncode != 0:
        raise RoundError(f"git add 失敗：{added.stderr.strip()[:300]}")
    if git(["diff", "--cached", "--quiet", "--", *files]).returncode == 0:
        finish_chapter(state, ch, "done", "完成（三個檔沒有變更，略過 commit）")
        return "無變更"
    message = f"工廠：新增 K143 {ch} 互動演練腳本（第 {ctx['round']} 輪）"
    committed = git(["commit", "-m", message, "--", *files])
    if committed.returncode != 0:
        raise RoundError(f"git commit 失敗：{(committed.stderr or committed.stdout).strip()[:300]}")
    finish_chapter(state, ch, "done", f"完成並 commit：{message}")
    return "已 commit"


# 依目前狀態決定下一站；judge／review 失敗會把狀態改回 planned，迴圈自然回到寫手
STATIONS = {
    "pending": ("plan", "1 規劃", station_plan),
    "planned": ("write", "2 寫腳本＋現況（平行）", station_write),
    "written": ("run", "3 執行腳本", station_run),
    "ran": ("judge", "4 測試判定", station_judge),
    "judged": ("review", "5 審查", station_review),
    "reviewed": ("commit", "6 commit", station_commit),
}


def maybe_fake_interrupt(station):
    """測試鉤子：Windows 很難從腳本對子程序送真正的 Ctrl+C，用這個模擬。"""
    if os.environ.get("LH_FAKE_INTERRUPT_AFTER") == station:
        print(f"    （測試鉤子）站 {station} 完成後模擬 Ctrl+C")
        raise KeyboardInterrupt


def run_stations(state, ch, ctx, summary):
    while state["chapters"][ch]["status"] not in FINAL_STATUSES:
        name, label, func = STATIONS[state["chapters"][ch]["status"]]
        print(f"[站 {label}]")
        start = time.monotonic()
        result = func(state, ch, ctx)
        summary.append((label, result, time.monotonic() - start))
        maybe_fake_interrupt(name)


# ---------- 一輪 ----------

def pick_chapter(state, chapter_arg):
    """指定章要在 queue 裡；沒指定就取第一個未結束的章。已結束的章不重跑。"""
    if chapter_arg:
        rec = state["chapters"].get(chapter_arg)
        if rec is None:
            raise RoundError(f"{chapter_arg} 不在 state.json，先 --init {chapter_arg}")
        if rec["status"] in FINAL_STATUSES:
            print(f"{chapter_arg} 已是 {rec['status']}；要重跑請 --init {chapter_arg} --force")
            return None
        return chapter_arg
    for ch in state["queue"]:
        if state["chapters"].get(ch, {}).get("status") not in FINAL_STATUSES:
            return ch
    return None


def begin_round(state, ch):
    """新章才開新一輪；接續中的章沿用原本的輪次與 claude -p 次數。"""
    rec = state["chapters"][ch]
    if rec["round"] is None:
        state["round"] += 1
        rec["round"] = state["round"]
        rec["started"] = now_str()
        log_event(state, ch, f"第 {rec['round']} 輪開始")
    else:
        log_event(state, ch, f"接續第 {rec['round']} 輪（目前站：{rec['status']}，已呼叫 {rec['calls']} 次）")
    return {"chapter": ch, "round": rec["round"]}


def write_heartbeat(state, ch, outcome, elapsed):
    rec = state["chapters"][ch]
    line = f"{now_str()}｜第 {rec['round']} 輪｜{ch}｜{outcome}｜claude -p {rec['calls']} 次｜{elapsed:.0f} 秒\n"
    HEARTBEAT_PATH.write_text(line, encoding="utf-8")


def print_summary(state, ch, summary, elapsed):
    rec = state["chapters"][ch]
    cost = rec["cost_usd"]
    print("\n" + "=" * 56)
    print(f"本輪摘要：第 {rec['round']} 輪｜{ch}｜狀態 {rec['status']}")
    for label, result, seconds in summary:
        print(f"  站 {label:<14} {result}（{seconds:.1f} 秒）")
    print(f"  claude -p 次數：{rec['calls']}／{MAX_CALLS_PER_ROUND}　重寫 {rec['rewrites']} 次　審查退回 {rec['review_retries']} 次")
    print(f"  等值成本：US$ {cost:.4f}（約 NT$ {cost * USD_TO_TWD:.1f}，走 Max 訂閱不實扣）")
    print(f"  本次耗時：{elapsed:.1f} 秒")
    print("=" * 56)


def cmd_run(chapter_arg):
    state = load_state()
    if state is None:
        raise RoundError("沒有 state.json，先執行 --init ch04 ch05 …")
    ch = pick_chapter(state, chapter_arg)
    if ch is None:
        if not chapter_arg:
            print("queue 裡沒有待做的章節")
        return 0
    ctx, start, summary = begin_round(state, ch), time.monotonic(), []
    try:
        run_stations(state, ch, ctx, summary)
    except KeyboardInterrupt:
        return handle_interrupt(state, ch, summary, time.monotonic() - start)
    except CallBudgetExceeded as exc:
        finish_chapter(state, ch, "failed", f"停止本輪：{exc}")
    except RoundError as exc:
        log_event(state, ch, f"錯誤（停在站 {state['chapters'][ch]['status']}，下次重試）：{exc}")
        write_heartbeat(state, ch, "失敗", time.monotonic() - start)
        print_summary(state, ch, summary, time.monotonic() - start)
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    elapsed = time.monotonic() - start
    write_heartbeat(state, ch, "成功" if state["chapters"][ch]["status"] == "done" else "失敗", elapsed)
    print_summary(state, ch, summary, elapsed)
    return 0 if state["chapters"][ch]["status"] == "done" else 1


def handle_interrupt(state, ch, summary, elapsed):
    """進度在每站結束時就存好了，這裡只補一筆紀錄與 heartbeat。"""
    status = state["chapters"][ch]["status"]
    try:
        log_event(state, ch, f"使用者中斷（停在站 {status}）")
        write_heartbeat(state, ch, "中斷", elapsed)
    except OSError as exc:
        print(f"（中斷時寫紀錄失敗：{exc}）", file=sys.stderr)
    print_summary(state, ch, summary, elapsed)
    print(f"\n⛔ 中斷！進度已存在 state.json（{ch} 停在 {status}），再執行一次會從這一站接著做")
    return 130


# ---------- 其他指令 ----------

def cmd_init(chapters, force):
    bad = [ch for ch in chapters if not CHAPTER_RE.match(ch)]
    if bad:
        raise RoundError(f"章節格式要是 chNN（例：ch04），收到：{bad}")
    state = load_state() or new_state()
    state["queue"] = list(dict.fromkeys(chapters))
    for ch in state["queue"]:
        if force or ch not in state["chapters"]:
            state["chapters"][ch] = new_chapter()
    log_event(state, "-", f"建立 queue：{' '.join(state['queue'])}{'（--force 重設）' if force else ''}")
    print(f"queue：{' '.join(state['queue'])}")
    return 0


def cmd_status():
    state = load_state()
    if state is None:
        print("沒有 state.json（尚未 --init）")
        return 0
    print(f"目前輪次：{state['round']}　queue：{' '.join(state['queue']) or '（空）'}")
    for ch in state["queue"]:
        rec = state["chapters"].get(ch, new_chapter())
        print(f"  {ch}  {rec['status']:<9} 輪 {rec['round'] or '-':<3} 呼叫 {rec['calls']:>2} 次  "
              f"重寫 {rec['rewrites']}  退回 {rec['review_retries']}  US$ {rec['cost_usd']:.4f}")
    print("最近 history：")
    for item in state["history"][-8:]:
        print(f"  {item['ts']} [第 {item['round']} 輪 {item['chapter']}] {item['event']}")
    if HEARTBEAT_PATH.exists():
        print("heartbeat：" + HEARTBEAT_PATH.read_text(encoding="utf-8").strip())
    return 0


def dry_run_agent(role, prompt, ctx):
    tools = load_role_tools(role)
    cmd = build_claude_cmd(role, "claude", RUNTIME_DIR / "hook-settings.json",
                           f"<agents/{role}.md 的內容>")
    env_note = f"LH_ROLE={role} LH_CHAPTER={ctx['chapter']} LH_ROUND={ctx['round']}"
    print(f"  [{role}] 工具：{','.join(tools)}")
    print(f"    {env_note} {shlex.join(cmd)}  < prompt（stdin）")
    print(f"    prompt 摘要（{len(prompt)} 字）：{prompt[:160].replace(chr(10), ' ')}…")


def cmd_dry_run(chapter_arg):
    """只印不跑：不寫 state、不呼叫模型、不執行 bash／git。"""
    state = load_state() or new_state()
    ch = chapter_arg or pick_chapter(state, None)
    if ch is None:
        raise RoundError("沒有可預覽的章節：先 --init 或加 --chapter chNN")
    rec = state["chapters"].get(ch) or new_chapter()
    ctx = {"chapter": ch, "round": rec["round"] or state["round"] + 1}
    paths = chapter_paths(ch)
    print(f"（dry-run）{ch} 第 {ctx['round']} 輪，cwd＝{posix(practice_dir())}，目前站：{rec['status']}")
    dry_run_agent("planner", prompt_plan(ch, paths), ctx)
    dry_run_agent("writer", prompt_write(ch, paths, rec), ctx)
    dry_run_agent("now-updater", prompt_now(ch, paths), ctx)
    print(f"  [run] cd {posix(paths['interactive'])} && bash -n {ch}.sh && bash {ch}.sh 1..8（每步逾時 {STEP_TIMEOUT_SEC} 秒）")
    dry_run_agent("tester", prompt_judge(ch, paths), ctx)
    dry_run_agent("reviewer", prompt_review(ch, paths), ctx)
    print(f"  [commit] git -C {posix(WORKTREE)} add -- {' '.join(commit_files(ch))}")
    print(f"           git commit -m \"工廠：新增 K143 {ch} 互動演練腳本（第 {ctx['round']} 輪）\" -- …（不 push）")
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(description="K143 演練備課工廠：一次執行＝一輪＝一章")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", nargs="+", metavar="chNN", help="建立／重設 queue")
    group.add_argument("--status", action="store_true", help="印出狀態，不呼叫模型")
    group.add_argument("--dry-run", action="store_true", help="印出每站指令與 prompt 摘要，不執行")
    parser.add_argument("--chapter", metavar="chNN", help="指定要跑的章")
    parser.add_argument("--force", action="store_true", help="搭配 --init：重設已有章節狀態")
    args = parser.parse_args(argv)
    if args.chapter and not CHAPTER_RE.match(args.chapter):
        parser.error(f"--chapter 格式要是 chNN，收到：{args.chapter}")
    return args


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    try:
        if args.init:
            return cmd_init(args.init, args.force)
        if args.status:
            return cmd_status()
        if args.dry_run:
            return cmd_dry_run(args.chapter)
        return cmd_run(args.chapter)
    except RoundError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"檔案操作錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
