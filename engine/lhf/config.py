"""讀取並驗證專案的 factory.yaml，並提供佔位符展開與路徑比對工具。

驗證分兩段：
1. 結構驗證（欄位型別、未知欄位、id 格式、站之間的參照）
2. 鐵律驗證（規格 §9）：站鏈必須是 plan? → producer → verify → qa → review → finish，
   撰寫／QA／審查必須是不同角色，QA 與審查的 write 路徑不可涵蓋撰寫者的產出。
任何錯誤都丟 ConfigError，訊息以「欄位路徑：原因」開頭，方便直接定位。
"""

import fnmatch
import os
import re
from pathlib import Path

import yaml

from lhf.errors import ConfigError, MissingVar

ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_-]*)\}")
VERDICT_WORD_RE = re.compile(r"^[A-Z]+$")
WILDCARD_CHARS = "*?["

ENGINE_DIR = Path(__file__).resolve().parents[1]
FACTORY_FILE = "factory.yaml"
BUILTIN_VARS = {"WORK", "RUN", "PROJECT", "HOME", "unit", "round", "step", "feedback"}
FINAL_STATUSES = frozenset({"done", "failed", "rejected"})

# 站鏈階段與允許的站型；順序就是鏈的先後（規格 §9）
CHAIN_ORDER = ("plan", "producer", "verify", "qa", "review", "finish")
CHAIN_KINDS = {
    "plan": {"agent", "parallel"},
    "producer": {"agent", "parallel"},
    "verify": {"command"},
    "qa": {"agent"},
    "review": {"agent"},
    "finish": {"commit", "approve"},
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
}
DEFAULT_FEEDBACK = (
    "\n\n這是重做。上一版沒有通過{feedback_label}，原因如下（來自 {feedback_path}）：\n"
    "-----\n{feedback_content}\n-----\n請只修正以上問題，其他部分保持不變。"
)

TOP_KEYS = {
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
}
STATION_KEYS = {
    "common": {"id", "kind", "chain", "label"},
    "agent": {"role", "prompt", "requires", "outputs", "clear", "verdict", "mkdirs", "on_fail"},
    "parallel": {"jobs", "requires", "mkdirs"},
    "command": {"cmds", "cwd", "timeout", "require", "log", "fail_report", "on_fail"},
    "commit": {"paths", "message"},
    "approve": {"message"},
}
JOB_KEYS = {"role", "prompt", "outputs", "once"}
CMD_KEYS = {"title", "cmd", "steps", "fail_on_error"}
ROLE_KEYS = {"tools", "write", "budget", "bash_allow"}


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
    """比對前統一：\\ 轉 /、轉小寫，讓 C:\\USERS 與 c:/users 視為同一路徑。"""
    return str(text).replace("\\", "/").lower()


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


# ---------- Config ----------


def is_fake():
    return os.environ.get("LH_FAKE_MODEL") == "1"


class Config:
    """factory.yaml 的唯讀包裝；所有路徑都由這裡推算，引擎其他地方不自己拼路徑。"""

    def __init__(self, project_dir, data):
        self.project_dir = Path(project_dir).resolve()
        self.data = data
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


def load_config(project_dir):
    """讀 <project_dir>/factory.yaml 並完整驗證；任何問題都丟 ConfigError。"""
    path = Path(project_dir) / FACTORY_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{FACTORY_FILE}：找不到 {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{FACTORY_FILE}：讀取失敗：{exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{FACTORY_FILE}：YAML 語法錯誤：{exc}") from exc
    validate_structure(data)
    cfg = Config(project_dir, data)
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
        need_str_list(spec.get("write") or [], f"{field}.write")
        if spec.get("budget") is not None:
            need_positive_int(spec["budget"], f"{field}.budget")
        if spec.get("bash_allow") is not None:
            need_regex_list(spec["bash_allow"], f"{field}.bash_allow")


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
        need(isinstance(item.get("fail_on_error", False), bool), f"{cmd_field}.fail_on_error", "必須是 true／false")
        if item.get("steps") is not None:
            validate_steps(item["steps"], f"{cmd_field}.steps")
    if station.get("timeout") is not None:
        need_positive_int(station["timeout"], f"{field}.timeout")
    need_str(station, "log", field)
    need_str(station, "fail_report", field)
    need_str(station, "cwd", field, required=False)
    validate_path_lists(station, field, ("require",))
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
            render_field(
                cfg.data[key], {**sample, "feedback_label": "", "feedback_path": "", "feedback_content": ""}, key
            )
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
