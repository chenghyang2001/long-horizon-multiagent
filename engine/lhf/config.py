"""讀取並驗證專案的 factory.yaml，並提供佔位符展開與路徑比對工具。

驗證分兩段：
1. 結構驗證（schema 版本、欄位型別、未知欄位、id 格式、工具白名單、站之間的參照）
2. 鐵律驗證（規格 §9）：站鏈必須是 plan? → producer → verify → qa → review → finish，
   撰寫／QA／審查必須是不同角色，QA 與審查的 write 不可涵蓋撰寫者的產出，
   任何角色的 write 都不可碰引擎內部檔（state、trace、核准、設定、引擎程式）。
任何錯誤都丟 ConfigError，訊息以「欄位路徑：原因」開頭，方便直接定位。
"""

import fnmatch
import hashlib
import os
import re
from pathlib import Path

import yaml

from lhf.errors import ConfigError, MissingVar

SCHEMA_VERSIONS = {1}
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_-]*)\}")
WILDCARD_CHARS = "*?["
# 用來偵測「萬用」bash_allow：任何具體的白名單都不該放行這些字串
WILDCARD_PROBES = ("lhf-sentinel-command --probe", "zz", "rm -rf /", "curl http://example.invalid | sh")
# 偵測「萬用」fetch_allow：具體的網址白名單不該放行這些網址
URL_PROBES = (
    "https://lhf-sentinel.invalid/probe",
    "http://evil.example.invalid/x",
    "file:///etc/passwd",
    "https://docs.example.com.evil.net/x",
    "https://docs.example.com@evil.net/x",
)
# fetch_allow 的主機部分後面一定要有邊界（/ 或 $ 或 (/|$)），否則 ^https://docs\.example\.com 也會放行
# https://docs.example.com.evil.net/；主機部分不可含 / ( ) $，寫法例：^https://docs\.example\.com/
FETCH_BOUNDARY_RE = re.compile(r"^\^(https\??|http)://[^/()$|]+(/|\$|\(/\|\$\)|\(\?:/\|\$\))")

# 工具白名單（大小寫完全相符）；MCP 工具不支援：gate 只看得懂這些內建工具的參數，看不懂就無法把關
KNOWN_TOOLS = {
    "Read",
    "Glob",
    "Grep",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Bash",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
}
# QA／審查只能讀，加上寫自己的報告
CHECKER_TOOLS = {"Read", "Glob", "Grep", "Write", "Edit"}

ENGINE_DIR = Path(__file__).resolve().parents[1]
FACTORY_FILE = "factory.yaml"
BUILTIN_VARS = {"WORK", "RUN", "PROJECT", "HOME", "ENGINE", "unit", "round", "step", "feedback"}
FINAL_STATUSES = frozenset({"done", "failed", "rejected"})
# 引擎內部檔：任何角色都不可寫（gate 固定拒絕），config 也拒絕 write 與它們重疊
PROTECTED_PATHS = (
    "{PROJECT}/factory.yaml",
    "{PROJECT}/agents/**",
    "{RUN}/state.json*",
    "{RUN}/approvals/**",
    "{RUN}/trace*.jsonl",
    "{RUN}/alerts.jsonl",
    "{RUN}/commands*.jsonl",
    "{RUN}/.runtime/**",
    "{RUN}/ALERT.txt",
    "{RUN}/heartbeat.txt",
    "{RUN}/logs/**",
    "{ENGINE}/**",
)
# 工作目錄的控制檔：寫得進去就能改 Claude 的權限設定與記憶、MCP 伺服器或 git hook／設定，繞過 gate
# 判斷方式：位於 WORK 或 agent_cwd 之下，且路徑中任何一段是 CONTROL_SEGMENTS（.git 可能是 worktree 的
# gitdir 檔，所以 .git 本身與底下都算），或檔名是 CLAUDE.md／CLAUDE.local.md（任何子目錄、不分大小寫）。
# gate 一律拒絕；config 只拒絕「明確指向」這些位置的 write（見 validate_protected）。CLAUDE.md 只能由人更新。
CONTROL_SEGMENTS = frozenset({".git", ".gitmodules", ".gitattributes", ".claude", ".mcp.json"})
CONTROL_FILENAMES = frozenset({"claude.md", "claude.local.md"})
DRIVE_RE = re.compile(r"^[a-z]:$")

# 站鏈階段與允許的站型；順序就是鏈的先後（規格 §9）
CHAIN_ORDER = ("plan", "producer", "verify", "qa", "review", "finish")
CHAIN_KINDS = {
    "plan": {"agent", "parallel"},
    "producer": {"agent", "parallel"},
    "verify": {"command"},
    "qa": {"agent"},
    "review": {"agent"},
    "finish": {"commit", "approve", "command"},  # 例：approve → command（部署／push）
}
CHAIN_VERDICTS = {"qa": ("PASS", "FAIL"), "review": ("PASS", "REJECT")}
# 退回規則固定由引擎決定（不可在 yaml 改計數器或最終狀態，避免繞過鐵律）
ON_FAIL_RULES = {
    "verify": {"counter": "rewrites", "status": "failed", "max": 2},
    "qa": {"counter": "rewrites", "status": "failed", "max": 2},
    "review": {"counter": "review_retries", "status": "rejected", "max": 1},
}

DEFAULT_LIMITS = {
    "calls_per_unit": 10,
    "agent_timeout": 900,
    "feedback_max_chars": 6000,
    "launch_retry_wait": 5,
    "command_timeout": 180,
    "max_engine_errors": 3,  # 同一單位連續引擎錯誤幾次就標 failed，換下一個單位
    "history_max": 2000,  # state.json 只保留最近幾筆 history
    "fail_logs_max": 50,  # logs/agent-fail-*.txt 只保留最近幾個
    "trace_archive_mb": 20,  # hours 開跑時 trace.jsonl 超過就歸檔
}
FEEDBACK_KEYS = (
    "feedback_label",
    "feedback_path",
    "feedback_content",
    "feedback_log",
    "feedback_log_content",
    "feedback_qa_section",
)
DEFAULT_FEEDBACK = (
    "\n\n這是重做。上一版沒有通過{feedback_label}，原因如下（來自 {feedback_path}）：\n"
    "-----\n{feedback_content}\n-----\n"
    "客觀驗證的原始紀錄（{feedback_log}，取檔尾）：\n-----\n{feedback_log_content}\n-----"
    "{feedback_qa_section}\n請只修正以上問題，其他部分保持不變。"
)

TOP_KEYS = {
    "schema",
    "name",
    "type",
    "description",
    "workdir",
    "branch",
    "agent_cwd",
    "read_dirs",
    "vars",
    "env",
    "units",
    "files",
    "extracts",
    "stations",
    "roles",
    "forbidden",
    "approval_commands",
    "limits",
    "prompts",
    "feedback_template",
    "fake",
    "notify",
    "bash",
}
STATION_KEYS = {
    "common": {"id", "kind", "chain", "label"},
    "agent": {"role", "prompt", "requires", "outputs", "clear", "verdict", "mkdirs", "on_fail"},
    "parallel": {"jobs", "requires", "mkdirs"},
    "command": {"cmds", "cwd", "timeout", "require", "log", "fail_report", "on_fail", "unattended"},
    "commit": {"paths", "message"},
    "approve": {"message"},
}
JOB_KEYS = {"role", "prompt", "outputs", "once"}
CMD_KEYS = {"title", "cmd", "steps", "fail_on_error", "allow_nonzero"}
ROLE_KEYS = {"tools", "write", "budget", "bash_allow", "fetch_allow"}


# ---------- 範本與路徑工具 ----------


def posix(path):
    return Path(path).as_posix()


def expand_home(text):
    """只展開開頭的 ~，其他字串原樣保留（prompt 片段也會經過這裡）。"""
    text = str(text)
    if text == "~" or text.startswith("~/") or text.startswith("~\\"):
        return posix(Path(os.path.expanduser(text)))
    return text


def render(template, mapping):
    """單次替換 {name}；{{ 與 }} 是跳脫。替換進來的值不會再被掃描，檔案內容含大括號也安全。"""

    def replace(match):
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name = match.group(1)
        if name not in mapping:
            raise MissingVar(name, template)
        return str(mapping[name])

    return PLACEHOLDER_RE.sub(replace, str(template))


def render_path(template, mapping):
    """展開佔位符後再展開開頭的 ~；所有「路徑」欄位都走這裡。"""
    return expand_home(render(template, mapping))


def norm_text(text):
    """比對前統一：反斜線轉 /、轉小寫，讓大小寫與分隔符號不同的同一路徑視為相同。"""
    return str(text).replace("\\", "/").lower()


def windows_clean(path_text):
    """照 Windows 的路徑語意正規化：每段結尾的「.」與空白會被系統忽略（.claude. 就是 .claude），
    「檔名:串流」的替代資料串流指向同一個檔（.git::$DATA 就是 .git）。先轉小寫、反斜線轉 /。"""
    parts = norm_text(path_text).split("/")
    cleaned = []
    for index, segment in enumerate(parts):
        if not (index == 0 and DRIVE_RE.match(segment)):
            segment = segment.split(":", 1)[0]
            if segment not in (".", ".."):
                segment = segment.rstrip(". ")
        cleaned.append(segment)
    return "/".join(cleaned)


def is_control_relative(segments):
    """相對於工作目錄的路徑段：任何一段是控制目錄／檔，或最後一段是 CLAUDE.md 類。"""
    segments = [s for s in segments if s]
    if not segments:
        return False
    return any(s in CONTROL_SEGMENTS for s in segments) or segments[-1] in CONTROL_FILENAMES


def literal_prefix(pattern):
    """萬用字元之前的固定前綴；用來判斷兩個 glob 會不會涵蓋同一批檔案。"""
    cut = min((pattern.find(ch) for ch in WILDCARD_CHARS if ch in pattern), default=len(pattern))
    return pattern[:cut]


def patterns_overlap(first, second):
    """保守判斷兩個（已正規化的）glob 是否可能命中同一個路徑；寧可誤判重疊也不漏判。"""
    if fnmatch.fnmatchcase(first, second) or fnmatch.fnmatchcase(second, first):
        return True
    first_wild = literal_prefix(first) != first
    second_wild = literal_prefix(second) != second
    if first_wild and literal_prefix(second).startswith(literal_prefix(first)):
        return True
    return second_wild and literal_prefix(first).startswith(literal_prefix(second))


def is_wsl_bash(path):
    """C:\\Windows\\System32\\bash.exe 是 WSL 的入口，不是 Git Bash；路徑、編碼都不同，一律拒絕。"""
    return "system32/bash" in norm_text(path) or "sysnative/bash" in norm_text(path)


# ---------- Config ----------


def is_fake():
    return os.environ.get("LH_FAKE_MODEL") == "1"


class Config:
    """factory.yaml 的唯讀包裝；所有路徑都由這裡推算，引擎其他地方不自己拼路徑。"""

    def __init__(self, project_dir, data, raw_text):
        self.project_dir = Path(project_dir).resolve()
        self.data = data
        self.raw_text = raw_text
        self.sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        self.run_dir = self.project_dir / "run"
        self.agents_dir = self.project_dir / "agents"
        self.stations = data["stations"]
        self.roles = data["roles"]
        self.limits = {**DEFAULT_LIMITS, **(data.get("limits") or {})}

    @property
    def name(self):
        return self.data["name"]

    @property
    def unit_ids(self):
        return list(self.data["units"]["ids"])

    @property
    def depends_on(self):
        return self.data["units"].get("depends_on") or {}

    def work_dir(self):
        """假模式一定不碰真的工作目錄：沒指定 override 就用 run/.fake-work。"""
        override = os.environ.get("LH_WORK_OVERRIDE")
        if override:
            return Path(override).resolve()
        if is_fake():
            return self.run_dir / ".fake-work"
        return Path(os.path.expanduser(self.data["workdir"])).resolve()

    def is_override(self):
        return is_fake() or bool(os.environ.get("LH_WORK_OVERRIDE"))

    def station(self, station_id):
        for station in self.stations:
            if station["id"] == station_id:
                return station
        raise ConfigError(f"stations：找不到站 {station_id!r}")

    def next_station_id(self, station_id):
        ids = [station["id"] for station in self.stations]
        index = ids.index(station_id) + 1
        return ids[index] if index < len(ids) else None

    def vars(self, unit=None, round_no=None, overrides=None):
        """組出佔位符對照表：內建 → vars → 單位變數 → files；沒有 unit 時不含 files。"""
        mapping = {
            "WORK": posix(self.work_dir()),
            "RUN": posix(self.run_dir),
            "PROJECT": posix(self.project_dir),
            "HOME": posix(Path.home()),
            "ENGINE": posix(ENGINE_DIR),
        }
        mapping.update(overrides or {})
        if round_no is not None:
            mapping["round"] = str(round_no)
        add_rendered(mapping, self.data.get("vars") or {})
        if unit is not None:
            mapping["unit"] = unit
            mapping.update(unit_vars(self.data["units"].get("vars") or {}, unit))
            add_rendered(mapping, self.data.get("files") or {})
        return mapping

    def chain_station(self, stage):
        return next(station for station in self.stations if station["chain"] == stage)

    def on_fail(self, station):
        """回傳合併了引擎固定規則的退回設定；沒有退回規則的站回 None。"""
        rules = ON_FAIL_RULES.get(station["chain"])
        if rules is None:
            return None
        merged = {**rules, **(station.get("on_fail") or {})}
        merged.setdefault("label", station.get("label") or station["id"])
        return merged

    def verdict_words(self, station):
        return CHAIN_VERDICTS[station["chain"]]

    def protected_patterns(self):
        """引擎內部檔（已展開、正規化）；gate 固定拒絕寫入這些位置。工作目錄控制檔另見 is_control_path。"""
        mapping = self.vars()
        return [norm_text(render_path(pattern, mapping)) for pattern in PROTECTED_PATHS]

    def control_bases(self):
        """WORK 與 agent_cwd（已正規化）；agent_cwd 需要單位變數時只保護 WORK（agent_cwd 通常在 WORK 底下）。"""
        mapping = self.vars()
        bases = [mapping["WORK"]]
        try:
            bases.append(render_path(self.data.get("agent_cwd") or "{WORK}", mapping))
        except MissingVar:
            pass
        return list(dict.fromkeys(windows_clean(posix(b)).rstrip("/") for b in bases))

    def is_control_path(self, target):
        """target（已正規化的絕對路徑）是否為工作目錄控制檔。"""
        target = windows_clean(target)
        for base in self.control_bases():
            if target.startswith(base + "/") and is_control_relative(target[len(base) + 1 :].split("/")):
                return True
        return False

    def round_time_limit(self):
        """hours 每輪總時限：所有 claude -p 都跑滿＋所有指令站在最多退回次數下都跑滿＋10 分鐘餘裕。"""
        agent_sec = self.limits["calls_per_unit"] * self.limits["agent_timeout"]
        attempts = 1 + max(rule["max"] for rule in (self.on_fail(self.chain_station(s)) for s in ("qa", "review")))
        command_sec = 0
        for station in self.stations:
            if station["kind"] == "command":
                count = sum(c["steps"][1] - c["steps"][0] + 1 if c.get("steps") else 1 for c in station["cmds"])
                command_sec += count * (station.get("timeout") or self.limits["command_timeout"])
        return agent_sec + command_sec * (attempts + 1) + 600

    def startup_warnings(self):
        warnings = []
        if self.data.get("notify"):
            warnings.append("notify 尚未實作，不會發通知（設定只做格式檢查）")
        return warnings


def add_rendered(mapping, items):
    for key, value in items.items():
        mapping[key] = expand_home(render(value, mapping))


def unit_vars(patterns, unit):
    """units.vars：名稱 → regex；有群組取第 1 群組，沒命中就用單位 id 本身。"""
    result = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, unit)
        if match is None:
            result[name] = unit
        else:
            result[name] = match.group(1) if match.groups() else match.group(0)
    return result


def read_factory(project_dir):
    path = Path(project_dir) / FACTORY_FILE
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"{FACTORY_FILE}：找不到 {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{FACTORY_FILE}：讀取失敗：{exc}") from exc


def load_config(project_dir, text=None):
    """讀 <project_dir>/factory.yaml（或給定的快照內容）並完整驗證；任何問題都丟 ConfigError。"""
    raw = read_factory(project_dir) if text is None else text
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{FACTORY_FILE}：YAML 語法錯誤：{exc}") from exc
    validate_structure(data)
    cfg = Config(project_dir, data, raw)
    validate_semantics(cfg)
    return cfg


# ---------- 結構驗證 ----------


def fail(field, message):
    raise ConfigError(f"{field}：{message}")


def need(condition, field, message):
    if not condition:
        fail(field, message)


def check_keys(obj, allowed, field):
    unknown = sorted(set(obj) - set(allowed))
    need(not unknown, field, f"未知欄位 {unknown}（允許：{sorted(allowed)}）")


def need_str(obj, key, field, required=True):
    value = obj.get(key)
    if value is None and not required:
        return None
    need(isinstance(value, str) and value.strip(), f"{field}.{key}" if field else key, "必須是非空字串")
    return value


def need_str_list(value, field, allow_empty=True):
    need(isinstance(value, list), field, "必須是清單")
    need(allow_empty or value, field, "不可為空")
    for index, item in enumerate(value):
        need(isinstance(item, str) and item.strip(), f"{field}[{index}]", "必須是非空字串")
    return value


def need_regex_list(value, field):
    need_str_list(value, field)
    for index, pattern in enumerate(value):
        try:
            re.compile(pattern)
        except re.error as exc:
            fail(f"{field}[{index}]", f"不是合法的 regex：{exc}")


def need_str_map(value, field, key_re=VAR_NAME_RE):
    need(isinstance(value, dict), field, "必須是物件（名稱: 值）")
    for key, item in value.items():
        need(isinstance(key, str) and key_re.match(key), f"{field}.{key}", "名稱格式不合法")
        need(isinstance(item, str), f"{field}.{key}", "值必須是字串（數字請加引號）")
    return value


def need_positive_int(value, field, allow_zero=False):
    ok = isinstance(value, int) and not isinstance(value, bool) and (value >= 0 if allow_zero else value > 0)
    need(ok, field, "必須是" + ("≥ 0" if allow_zero else "> 0") + " 的整數")


def validate_structure(data):
    need(isinstance(data, dict), "（根）", f"{FACTORY_FILE} 必須是 YAML 物件")
    check_keys(data, TOP_KEYS, "（根）")
    need(
        data.get("schema") in SCHEMA_VERSIONS,
        "schema",
        f"必須是支援的版本 {sorted(SCHEMA_VERSIONS)}（收到 {data.get('schema')!r}）；新版設定請升級引擎",
    )
    if data.get("bash") is not None:
        need_str(data, "bash", "")
        need(not is_wsl_bash(data["bash"]), "bash", "指到 System32 的 bash.exe 是 WSL，請改指 Git Bash 的 bash.exe")
    need_str(data, "name", "")
    need(ID_RE.match(data["name"]), "name", "只能用英數字、底線、連字號")
    need_str(data, "workdir", "")
    for key in ("type", "description", "branch", "agent_cwd", "feedback_template"):
        need_str(data, key, "", required=False)
    validate_maps(data)
    validate_units(data.get("units"))
    validate_roles(data.get("roles"))
    validate_stations(data.get("stations"), data["roles"], data.get("prompts") or {})
    validate_policy(data)


def validate_maps(data):
    for key in ("vars", "files", "env", "prompts"):
        if data.get(key) is not None:
            need_str_map(data[key], key)
    for key in ("vars", "files"):
        clash = sorted(set(data.get(key) or {}) & BUILTIN_VARS)
        need(not clash, key, f"不可覆寫內建佔位符 {clash}")
    clash = sorted(set(data.get("vars") or {}) & set(data.get("files") or {}))
    need(not clash, "files", f"與 vars 重名：{clash}")
    if data.get("read_dirs") is not None:
        need_str_list(data["read_dirs"], "read_dirs")
    validate_extracts(data.get("extracts"))
    if data.get("fake") is not None:
        need(isinstance(data["fake"], dict), "fake", "必須是物件（角色: {檔案範本: 內容}）")
        for role, files in data["fake"].items():
            need_str_map(files, f"fake.{role}", key_re=re.compile(r".+"))
    if data.get("notify") is not None:
        need(isinstance(data["notify"], dict), "notify", "必須是物件")


def validate_extracts(extracts):
    if extracts is None:
        return
    need(isinstance(extracts, dict), "extracts", "必須是物件")
    for name, spec in extracts.items():
        field = f"extracts.{name}"
        need(VAR_NAME_RE.match(str(name)), field, "名稱格式不合法")
        need(isinstance(spec, dict), field, "必須是物件 {file, regex, default}")
        check_keys(spec, {"file", "regex", "default"}, field)
        need_str(spec, "file", field)
        need_str(spec, "regex", field)
        need(isinstance(spec.get("default", ""), str), f"{field}.default", "必須是字串")


def validate_units(units):
    need(isinstance(units, dict), "units", "必須是物件 {ids, depends_on, vars}")
    check_keys(units, {"ids", "depends_on", "vars"}, "units")
    ids = need_str_list(units.get("ids"), "units.ids", allow_empty=False)
    for index, unit in enumerate(ids):
        need(ID_RE.match(unit), f"units.ids[{index}]", f"{unit!r} 只能用英數字、底線、連字號")
    need(len(set(ids)) == len(ids), "units.ids", "有重複的單位 id")
    if units.get("vars") is not None:
        need_str_map(units["vars"], "units.vars")
        need_regex_list(list(units["vars"].values()), "units.vars")
    validate_depends(units.get("depends_on") or {}, set(ids))


def validate_depends(depends, ids):
    need(isinstance(depends, dict), "units.depends_on", "必須是物件 {單位: [依賴的單位]}")
    for unit, deps in depends.items():
        field = f"units.depends_on.{unit}"
        need(unit in ids, field, "不在 units.ids")
        need_str_list(deps, field)
        for dep in deps:
            need(dep in ids, field, f"依賴的 {dep!r} 不在 units.ids")
            need(dep != unit, field, "不可依賴自己")
    cycle = find_cycle(depends)
    need(cycle is None, "units.depends_on", f"有循環依賴：{' → '.join(cycle or [])}")


def find_cycle(depends):
    """DFS 找循環；回傳循環路徑或 None。"""
    color, stack = {}, []

    def visit(node):
        color[node] = "gray"
        stack.append(node)
        for dep in depends.get(node) or []:
            if color.get(dep) == "gray":
                return stack[stack.index(dep) :] + [dep]
            if dep not in color and (found := visit(dep)):
                return found
        stack.pop()
        color[node] = "black"
        return None

    for node in depends:
        if node not in color and (found := visit(node)):
            return found
    return None


def validate_roles(roles):
    need(isinstance(roles, dict) and roles, "roles", "必須是非空物件")
    for name, spec in roles.items():
        field = f"roles.{name}"
        need(isinstance(name, str) and ID_RE.match(name), field, "角色名稱只能用英數字、底線、連字號")
        need(isinstance(spec, dict), field, "必須是物件 {tools, write, budget}")
        check_keys(spec, ROLE_KEYS, field)
        need_str_list(spec.get("tools"), f"{field}.tools", allow_empty=False)
        validate_tool_names(spec["tools"], f"{field}.tools")
        need_str_list(spec.get("write") or [], f"{field}.write")
        if spec.get("budget") is not None:
            need_positive_int(spec["budget"], f"{field}.budget")
        if spec.get("bash_allow") is not None:
            need_regex_list(spec["bash_allow"], f"{field}.bash_allow")
        validate_bash_policy(spec, field)
        validate_fetch_policy(spec, field)


def validate_fetch_policy(spec, field):
    """WebFetch 能把資料送到任意網址：有 WebFetch 就必須有以 ^ 錨定、不可萬用的網址白名單。"""
    allow = spec.get("fetch_allow")
    if "WebFetch" not in spec["tools"]:
        need(allow is None, f"{field}.fetch_allow", "沒有 WebFetch 工具卻設了 fetch_allow")
        return
    need(
        isinstance(allow, list) and allow,
        f"{field}.fetch_allow",
        "有 WebFetch 就必須設 fetch_allow（網址 regex 清單，開頭用 ^）",
    )
    need_regex_list(allow, f"{field}.fetch_allow")
    for index, pattern in enumerate(allow):
        item = f"{field}.fetch_allow[{index}]"
        need(pattern.startswith("^"), item, f"必須以 ^ 錨定開頭（收到 {pattern!r}）")
        need(
            FETCH_BOUNDARY_RE.match(pattern),
            item,
            f"主機名稱後面必須有邊界 / 或 $ 或 (/|$)（收到 {pattern!r}），例：^https://docs[.]example[.]com/",
        )
        hits = [probe for probe in URL_PROBES if re.search(pattern, probe)]
        if hits:
            fail(
                item, f"{pattern!r} 太寬（連 {hits[0]!r} 都放行），請寫成指定網域，例如 ^https://docs\\.example\\.com/"
            )


def validate_tool_names(tools, field):
    for index, tool in enumerate(tools):
        if tool.startswith("mcp__"):
            fail(f"{field}[{index}]", f"不支援 MCP 工具 {tool}：gate 看不懂它的參數，無法檢查寫入路徑與指令，等於放行")
        need(
            tool in KNOWN_TOOLS,
            f"{field}[{index}]",
            f"未知工具 {tool!r}（大小寫要完全相符，可用：{sorted(KNOWN_TOOLS)}）",
        )
    need(len(set(tools)) == len(tools), field, "工具重複")


def validate_bash_policy(spec, field):
    """Bash 安全只靠 bash_allow 白名單：有 Bash 就必須有白名單，且每條都要錨定開頭、不可是萬用 pattern。"""
    allow = spec.get("bash_allow") or []
    if "Bash" not in spec["tools"]:
        need(not allow, f"{field}.bash_allow", "沒有 Bash 工具卻設了 bash_allow（請一併移除，避免誤以為有效）")
        return
    need(allow, f"{field}.bash_allow", "有 Bash 工具就必須設 bash_allow 白名單（regex，開頭用 ^）")
    for index, pattern in enumerate(allow):
        item = f"{field}.bash_allow[{index}]"
        need(pattern.startswith("^"), item, f"必須以 ^ 錨定開頭（收到 {pattern!r}），否則會命中指令中間的任何片段")
        hits = [probe for probe in WILDCARD_PROBES if re.search(pattern, probe)]
        if hits:
            fail(item, f"{pattern!r} 太寬（連 {hits[0]!r} 都放行），不可用 .*／.+ 這類萬用 pattern")


def validate_stations(stations, roles, prompts):
    need(isinstance(stations, list) and stations, "stations", "必須是非空清單")
    seen = set()
    for index, station in enumerate(stations):
        field = f"stations[{index}]"
        need(isinstance(station, dict), field, "必須是物件")
        need_str(station, "id", field)
        need(ID_RE.match(station["id"]), f"{field}.id", "只能用英數字、底線、連字號")
        need(station["id"] not in seen, f"{field}.id", f"重複的站 id {station['id']!r}")
        seen.add(station["id"])
        validate_station(station, f"{field}({station['id']})", roles, prompts)
    validate_gotos(stations)


def validate_station(station, field, roles, prompts):
    kind = station.get("kind")
    need(
        kind in STATION_KEYS and kind != "common",
        f"{field}.kind",
        f"必須是 agent／parallel／command／commit／approve，收到 {kind!r}",
    )
    check_keys(station, STATION_KEYS["common"] | STATION_KEYS[kind], field)
    chain = station.get("chain")
    need(chain in CHAIN_ORDER, f"{field}.chain", f"必須是 {'／'.join(CHAIN_ORDER)} 之一，收到 {chain!r}")
    need(
        kind in CHAIN_KINDS[chain], f"{field}.chain", f"{chain} 站只能是 {sorted(CHAIN_KINDS[chain])}，收到 kind={kind}"
    )
    if station.get("label") is not None:
        need_str(station, "label", field)
    VALIDATORS[kind](station, field, roles, prompts)


def validate_role_ref(role, field, roles):
    need(isinstance(role, str) and role in roles, field, f"角色 {role!r} 沒有在 roles 定義")


def validate_prompt_ref(prompt, field, prompts):
    need(isinstance(prompt, str) and prompt in prompts, field, f"prompts 沒有 {prompt!r}")


def validate_path_lists(station, field, keys):
    for key in keys:
        if station.get(key) is not None:
            need_str_list(station[key], f"{field}.{key}")


def validate_agent(station, field, roles, prompts):
    validate_role_ref(station.get("role"), f"{field}.role", roles)
    validate_prompt_ref(station.get("prompt"), f"{field}.prompt", prompts)
    validate_path_lists(station, field, ("requires", "outputs", "clear", "mkdirs"))
    has_verdict = station["chain"] in CHAIN_VERDICTS
    if not has_verdict:
        need("verdict" not in station, f"{field}.verdict", f"{station['chain']} 站不可有 verdict")
        need("on_fail" not in station, f"{field}.on_fail", f"{station['chain']} 站不可有 on_fail")
        return
    verdict = station.get("verdict")
    need(isinstance(verdict, dict), f"{field}.verdict", "qa／review 站必須有 verdict {file}")
    check_keys(verdict, {"file", "allowed"}, f"{field}.verdict")
    need_str(verdict, "file", f"{field}.verdict")
    expected = list(CHAIN_VERDICTS[station["chain"]])
    allowed = verdict.get("allowed", expected)
    need(allowed == expected, f"{field}.verdict.allowed", f"{station['chain']} 站固定為 {expected}，收到 {allowed}")
    validate_on_fail(station, field, required=True)


def validate_parallel(station, field, roles, prompts):
    jobs = station.get("jobs")
    need(isinstance(jobs, list) and jobs, f"{field}.jobs", "必須是非空清單")
    seen = set()
    for index, job in enumerate(jobs):
        job_field = f"{field}.jobs[{index}]"
        need(isinstance(job, dict), job_field, "必須是物件 {role, prompt}")
        check_keys(job, JOB_KEYS, job_field)
        validate_role_ref(job.get("role"), f"{job_field}.role", roles)
        validate_prompt_ref(job.get("prompt"), f"{job_field}.prompt", prompts)
        need(job["role"] not in seen, f"{job_field}.role", "同一站的平行工作不可重複角色（trace 會分不清）")
        seen.add(job["role"])
        if job.get("outputs") is not None:
            need_str_list(job["outputs"], f"{job_field}.outputs")
        need(isinstance(job.get("once", False), bool), f"{job_field}.once", "必須是 true／false")
    validate_path_lists(station, field, ("requires", "mkdirs"))


def validate_command(station, field, roles, prompts):
    cmds = station.get("cmds")
    need(isinstance(cmds, list) and cmds, f"{field}.cmds", "必須是非空清單")
    for index, item in enumerate(cmds):
        cmd_field = f"{field}.cmds[{index}]"
        need(isinstance(item, dict), cmd_field, "必須是物件 {title, cmd, steps, fail_on_error}")
        check_keys(item, CMD_KEYS, cmd_field)
        need_str(item, "cmd", cmd_field)
        need_str(item, "title", cmd_field, required=False)
        for flag in ("fail_on_error", "allow_nonzero"):
            need(isinstance(item.get(flag, False), bool), f"{cmd_field}.{flag}", "必須是 true／false")
        if item.get("steps") is not None:
            validate_steps(item["steps"], f"{cmd_field}.steps")
    if station.get("timeout") is not None:
        need_positive_int(station["timeout"], f"{field}.timeout")
    need_str(station, "log", field)
    need_str(station, "cwd", field, required=False)
    validate_path_lists(station, field, ("require",))
    need(isinstance(station.get("unattended", False), bool), f"{field}.unattended", "必須是 true／false")
    if station["chain"] == "finish":
        # 收尾站（部署／push）失敗不退回撰寫者：直接標 failed 並寫 ALERT，要人處理
        need("on_fail" not in station, f"{field}.on_fail", "finish 鏈的 command 站不可有 on_fail")
        need_str(station, "fail_report", field, required=False)
        return
    need("unattended" not in station, f"{field}.unattended", "只有 finish 鏈的 command 站可以設 unattended")
    need_str(station, "fail_report", field)
    validate_on_fail(station, field, required=True)


def validate_steps(steps, field):
    ok = (
        isinstance(steps, list)
        and len(steps) == 2
        and all(isinstance(n, int) and not isinstance(n, bool) for n in steps)
        and steps[0] <= steps[1]
    )
    need(ok, field, "必須是 [起, 迄] 兩個整數且起 ≤ 迄")


def validate_commit(station, field, roles, prompts):
    paths = need_str_list(station.get("paths"), f"{field}.paths", allow_empty=False)
    for index, path in enumerate(paths):
        text = path.replace("\\", "/")
        bad = text.startswith("/") or re.match(r"^[A-Za-z]:", text) or ".." in text.split("/")
        need(not bad, f"{field}.paths[{index}]", "必須是 workdir 底下的相對路徑，不可含 ..")
    need_str(station, "message", field, required=False)


def validate_approve(station, field, roles, prompts):
    need_str(station, "message", field, required=False)


VALIDATORS = {
    "agent": validate_agent,
    "parallel": validate_parallel,
    "command": validate_command,
    "commit": validate_commit,
    "approve": validate_approve,
}


def validate_on_fail(station, field, required):
    on_fail = station.get("on_fail")
    if on_fail is None:
        need(not required, f"{field}.on_fail", "verify／qa／review 站必須有 on_fail {goto}")
        return
    need(isinstance(on_fail, dict), f"{field}.on_fail", "必須是物件 {goto, max, label}")
    check_keys(on_fail, {"goto", "max", "label"}, f"{field}.on_fail")
    need_str(on_fail, "goto", f"{field}.on_fail")
    if on_fail.get("max") is not None:
        need_positive_int(on_fail["max"], f"{field}.on_fail.max", allow_zero=True)
    need_str(on_fail, "label", f"{field}.on_fail", required=False)


def validate_gotos(stations):
    """退回只能回到 producer 站：這樣 REJECT 之後一定重新經過 verify＋qa 才能再進 review。"""
    producers = {s["id"] for s in stations if s["chain"] == "producer"}
    for index, station in enumerate(stations):
        goto = (station.get("on_fail") or {}).get("goto")
        if goto is not None:
            need(
                goto in producers,
                f"stations[{index}]({station['id']}).on_fail.goto",
                f"只能退回 producer 站 {sorted(producers)}，收到 {goto!r}",
            )


def validate_policy(data):
    forbidden = data.get("forbidden") or {}
    need(isinstance(forbidden, dict), "forbidden", "必須是物件 {paths, commands}")
    check_keys(forbidden, {"paths", "commands"}, "forbidden")
    need_str_list(forbidden.get("paths") or [], "forbidden.paths")
    need_str_list(forbidden.get("commands") or [], "forbidden.commands")
    need_regex_list(data.get("approval_commands") or [], "approval_commands")
    limits = data.get("limits") or {}
    need(isinstance(limits, dict), "limits", "必須是物件")
    check_keys(limits, set(DEFAULT_LIMITS), "limits")
    for key, value in limits.items():
        need_positive_int(value, f"limits.{key}")


# ---------- 鐵律驗證（規格 §9） ----------


def validate_semantics(cfg):
    validate_chain(cfg.stations)
    validate_agents_files(cfg)
    validate_templates(cfg)  # 先確認範本都展得開，後面的路徑重疊檢查才有意義
    validate_separation(cfg)
    validate_protected(cfg)
    validate_rewrite_limits(cfg)
    validate_finish_commands(cfg.stations)
    validate_checker_vs_logs(cfg)


def validate_protected(cfg):
    """任何角色的 write 都不可涵蓋引擎內部檔，否則代理能改 state、核准紀錄或設定來繞過鐵律。
    工作目錄控制檔（.claude／.mcp.json／.git）只拒絕「明確指向」它們的 write：像 {WORK}/** 這種寬寫法
    在專案裡很常見（整個工作目錄都是產出），一律擋掉會讓設定寫不出來；這類寫法由 gate 逐筆固定拒絕。"""
    mapping = cfg.vars(unit=cfg.unit_ids[0], round_no=1)
    engine_files = [norm_text(render_path(p, cfg.vars())) for p in PROTECTED_PATHS]
    for role in cfg.roles:
        for pattern in rendered_patterns(cfg, role, mapping):
            for guarded in engine_files:
                need(
                    not patterns_overlap(pattern, guarded),
                    f"roles.{role}.write",
                    f"與引擎內部檔重疊（{pattern} ↔ {guarded}）：代理不可寫 state、trace、核准、設定或引擎程式",
                )
            need(
                not points_into_control(cfg, pattern),
                f"roles.{role}.write",
                f"明確指向工作目錄控制檔（{pattern}）：.git／.gitmodules／.gitattributes／.claude／.mcp.json／"
                "CLAUDE.md 一律不可寫（CLAUDE.md 只能由人更新）",
            )


def points_into_control(cfg, pattern):
    """把含萬用字元的段換成一般名稱再判斷：{WORK}/** 不算明確指向，{WORK}/**/CLAUDE.md、{WORK}/.git 算。"""
    plain = "/".join("x" if any(ch in segment for ch in WILDCARD_CHARS) else segment for segment in pattern.split("/"))
    return cfg.is_control_path(plain)


def validate_finish_commands(stations):
    """finish 鏈的 command 站（push／部署）前面必須有 approve 站；確定要無人值守才標 unattended: true。"""
    approved = False
    for index, station in enumerate(stations):
        if station["chain"] != "finish":
            continue
        approved = approved or station["kind"] == "approve"
        if station["kind"] == "command":
            need(
                approved or station.get("unattended") is True,
                f"stations[{index}]({station['id']})",
                "finish 鏈的 command 站前面必須有 approve 站；確定要無人值守執行請標 unattended: true",
            )


def validate_checker_vs_logs(cfg):
    """QA／審查不可寫 command 站的 raw log：否則能改掉客觀驗證的結果再自己判 PASS。"""
    mapping = cfg.vars(unit=cfg.unit_ids[0], round_no=1)
    logs = [norm_text(render_path(s["log"], mapping)) for s in cfg.stations if s["kind"] == "command"]
    for stage in ("qa", "review"):
        role = cfg.chain_station(stage)["role"]
        for pattern in rendered_patterns(cfg, role, mapping):
            for log in logs:
                need(
                    not fnmatch.fnmatchcase(log, pattern),
                    f"roles.{role}.write",
                    f"涵蓋了指令站的 raw log {log}：QA／審查不可改客觀驗證的紀錄",
                )


def validate_rewrite_limits(cfg):
    """verify 與 qa 共用 rewrites 計數器，上限不同會讓「誰先用完」變得難以預期。"""
    verify_max = cfg.on_fail(cfg.chain_station("verify"))["max"]
    qa_max = cfg.on_fail(cfg.chain_station("qa"))["max"]
    need(
        verify_max == qa_max,
        "stations(verify).on_fail.max",
        f"verify（{verify_max}）與 qa（{qa_max}）的 on_fail.max 必須相同（共用 rewrites 計數器）",
    )


def validate_chain(stations):
    """鏈的順序不可倒退，且 producer／verify／qa／review／finish 每一環都要有。"""
    stages = [station["chain"] for station in stations]
    ranks = [CHAIN_ORDER.index(stage) for stage in stages]
    for index in range(1, len(ranks)):
        need(
            ranks[index] >= ranks[index - 1],
            f"stations[{index}]({stations[index]['id']}).chain",
            f"{stages[index]} 不可排在 {stages[index - 1]} 之後（順序必須是 {' → '.join(CHAIN_ORDER)}）",
        )
    missing = [stage for stage in ("producer", "verify", "qa", "review", "finish") if stage not in stages]
    need(
        not missing, "stations", f"鐵律站鏈缺少：{'、'.join(missing)}（必須 producer → verify → qa → review → finish）"
    )
    for stage in ("verify", "qa", "review"):
        need(stages.count(stage) == 1, "stations", f"{stage} 站必須剛好一個，目前 {stages.count(stage)} 個")


def producer_roles(cfg):
    roles = []
    for station in cfg.stations:
        if station["chain"] != "producer":
            continue
        roles += [station["role"]] if station["kind"] == "agent" else [job["role"] for job in station["jobs"]]
    return roles


def validate_separation(cfg):
    """撰寫／QA／審查不同角色；QA、審查者的 write 不可涵蓋撰寫者產出。"""
    producers = producer_roles(cfg)
    qa_role = cfg.chain_station("qa")["role"]
    review_role = cfg.chain_station("review")["role"]
    need(qa_role not in producers, "stations(qa).role", f"QA 角色 {qa_role} 不可同時是撰寫者")
    need(review_role not in producers, "stations(review).role", f"審查角色 {review_role} 不可同時是撰寫者")
    need(qa_role != review_role, "stations(review).role", f"QA 與審查不可是同一角色（{qa_role}）")
    for checker in (qa_role, review_role):
        spec = cfg.roles[checker]
        banned = sorted(set(spec["tools"]) - CHECKER_TOOLS)
        need(
            not banned,
            f"roles.{checker}.tools",
            f"QA／審查角色只能用 {sorted(CHECKER_TOOLS)}，不可有 {banned}（只能讀與寫自己的報告）",
        )
        need(not spec.get("bash_allow"), f"roles.{checker}.bash_allow", "QA／審查角色不可有 bash_allow")
    mapping = cfg.vars(unit=cfg.unit_ids[0], round_no=1)
    producer_patterns = [(role, pattern) for role in producers for pattern in rendered_patterns(cfg, role, mapping)]
    for checker in (qa_role, review_role):
        for pattern in rendered_patterns(cfg, checker, mapping):
            for role, other in producer_patterns:
                need(
                    not patterns_overlap(pattern, other),
                    f"roles.{checker}.write",
                    f"與撰寫者 {role} 的 write 重疊（{pattern} ↔ {other}）：QA／審查只能寫自己的報告",
                )
    validate_verdict_writable(cfg, mapping)


def rendered_patterns(cfg, role, mapping):
    result = []
    for index, pattern in enumerate(cfg.roles[role].get("write") or []):
        try:
            result.append(norm_text(render_path(pattern, mapping)))
        except MissingVar as exc:
            fail(f"roles.{role}.write[{index}]", str(exc))
    return result


def validate_verdict_writable(cfg, mapping):
    """判定檔要落在 QA／審查角色自己的 write 內，否則 gate 會擋下、永遠讀不到 VERDICT。"""
    for stage in ("qa", "review"):
        station = cfg.chain_station(stage)
        target = norm_text(
            expand_home(render_field(station["verdict"]["file"], mapping, f"stations({station['id']}).verdict.file"))
        )
        patterns = rendered_patterns(cfg, station["role"], mapping)
        need(
            any(fnmatch.fnmatchcase(target, p) for p in patterns),
            f"stations({station['id']}).verdict.file",
            f"不在角色 {station['role']} 的 write 範圍內",
        )


def render_field(template, mapping, field):
    try:
        return render(template, mapping)
    except MissingVar as exc:
        raise ConfigError(f"{field}：{exc}") from exc


def used_roles(cfg):
    roles = set()
    for station in cfg.stations:
        if station["kind"] == "agent":
            roles.add(station["role"])
        elif station["kind"] == "parallel":
            roles.update(job["role"] for job in station["jobs"])
    return roles


def validate_agents_files(cfg):
    for role in sorted(used_roles(cfg)):
        path = cfg.agents_dir / f"{role}.md"
        need(path.is_file(), f"roles.{role}", f"缺少角色說明 {posix(path)}")


def validate_templates(cfg):
    """用第一個單位實際展開一次所有範本，typo 在啟動前就抓出來。"""
    try:
        cfg.vars()
    except MissingVar as exc:
        raise ConfigError(f"vars：{exc}（vars 不可用 {{unit}}，與單位有關的請放 files）") from exc
    sample = cfg.vars(unit=cfg.unit_ids[0], round_no=1)
    sample.update({"feedback": "", "step": "1"})
    sample.update({name: "" for name in cfg.data.get("extracts") or {}})
    for name, template in (cfg.data.get("prompts") or {}).items():
        render_field(template, sample, f"prompts.{name}")
    for name, spec in (cfg.data.get("extracts") or {}).items():
        render_field(spec["file"], sample, f"extracts.{name}.file")
    for index, station in enumerate(cfg.stations):
        validate_station_templates(station, sample, f"stations[{index}]({station['id']})")
    for key in ("agent_cwd", "feedback_template"):
        if cfg.data.get(key):
            render_field(cfg.data[key], {**sample, **{name: "" for name in FEEDBACK_KEYS}}, key)
    for index, item in enumerate(cfg.data.get("read_dirs") or []):
        render_field(item, sample, f"read_dirs[{index}]")


def validate_station_templates(station, sample, field):
    for key in ("requires", "outputs", "clear", "mkdirs", "require", "paths"):
        for index, item in enumerate(station.get(key) or []):
            render_field(item, sample, f"{field}.{key}[{index}]")
    for key in ("log", "fail_report", "cwd", "message"):
        if station.get(key):
            render_field(station[key], sample, f"{field}.{key}")
    if station.get("verdict"):
        render_field(station["verdict"]["file"], sample, f"{field}.verdict.file")
    for index, item in enumerate(station.get("cmds") or []):
        render_field(item["cmd"], sample, f"{field}.cmds[{index}].cmd")
    for index, job in enumerate(station.get("jobs") or []):
        for out_index, item in enumerate(job.get("outputs") or []):
            render_field(item, sample, f"{field}.jobs[{index}].outputs[{out_index}]")
