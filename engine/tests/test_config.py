"""config 驗證：合法設定可載入；各種錯誤都拒絕啟動，且訊息指出欄位。"""

import copy

import pytest

from conftest import demo_config, write_project
from lhf.config import load_config, patterns_overlap, render
from lhf.errors import ConfigError, MissingVar


def load_with(tmp_path, mutate):
    data = copy.deepcopy(demo_config())
    mutate(data)
    return load_config(write_project(tmp_path, data))


def expect_error(tmp_path, mutate, *fragments):
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, mutate)
    message = str(info.value)
    for fragment in fragments:
        assert fragment in message, message
    return message


def test_k143_loads(k143_dir):
    cfg = load_config(k143_dir)
    assert cfg.unit_ids[0] == "ch04" and len(cfg.unit_ids) == 15
    assert [s["chain"] for s in cfg.stations] == ["plan", "producer", "verify", "qa", "review", "finish"]
    assert cfg.vars(unit="ch10")["num"] == "10"


def test_demo_loads(demo_dir):
    assert load_config(demo_dir).name == "demo"


def test_missing_factory_file(tmp_path):
    with pytest.raises(ConfigError, match="找不到"):
        load_config(tmp_path)


def test_yaml_syntax_error(tmp_path):
    (tmp_path / "factory.yaml").write_text("name: [x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML 語法錯誤"):
        load_config(tmp_path)


def test_missing_workdir(tmp_path):
    expect_error(tmp_path, lambda d: d.pop("workdir"), "workdir")


def test_unknown_top_key(tmp_path):
    expect_error(tmp_path, lambda d: d.update(skip_qa=True), "未知欄位", "skip_qa")


def test_invalid_unit_id(tmp_path):
    expect_error(tmp_path, lambda d: d["units"].update(ids=["a/b"]), "units.ids[0]")


def test_dependency_cycle(tmp_path):
    expect_error(tmp_path, lambda d: d["units"].update(depends_on={"a": ["b"], "b": ["a"]}), "循環依賴")


def test_unknown_dependency(tmp_path):
    expect_error(tmp_path, lambda d: d["units"].update(depends_on={"a": ["zzz"]}), "units.depends_on.a")


def test_missing_qa_station(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"].pop(2), "缺少", "qa")


def test_missing_review_station(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"].pop(3), "缺少", "review")


def test_missing_verify_station(tmp_path):
    def mutate(data):
        data["stations"].pop(1)

    expect_error(tmp_path, mutate, "verify")


def test_missing_finish_station(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"].pop(4), "缺少", "finish")


def test_chain_order_wrong(tmp_path):
    def mutate(data):
        stations = data["stations"]
        stations[2], stations[3] = stations[3], stations[2]

    expect_error(tmp_path, mutate, ".chain", "不可排在")


def test_verify_must_be_command(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"][0].update(chain="verify"), "stations[0](make).chain")


def test_qa_role_same_as_producer(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"][2].update(role="maker"), "QA 角色 maker 不可同時是撰寫者")


def test_review_role_same_as_producer(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"][3].update(role="maker"), "審查角色 maker")


def test_qa_and_review_same_role(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"][3].update(role="qa"), "QA 與審查不可是同一角色")


def test_qa_write_overlaps_producer(tmp_path):
    expect_error(tmp_path, lambda d: d["roles"]["qa"]["write"].append("{WORK}/**"), "roles.qa.write", "重疊")


def test_review_write_overlaps_producer(tmp_path):
    expect_error(tmp_path, lambda d: d["roles"]["rv"]["write"].append("{out}"), "roles.rv.write", "重疊")


def test_verdict_file_outside_role_write(tmp_path):
    expect_error(
        tmp_path,
        lambda d: d["stations"][2]["verdict"].update(file="{RUN}/elsewhere/{unit}.md"),
        "verdict.file",
        "write 範圍",
    )


def test_goto_must_be_producer(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"][3]["on_fail"].update(goto="qa"), "on_fail.goto", "producer")


def test_qa_requires_on_fail(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"][2].pop("on_fail"), "stations[2](qa).on_fail")


def test_on_fail_cannot_override_counter_or_status(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"][3]["on_fail"].update(status="done"), "未知欄位")


def test_verdict_words_fixed(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"][3]["verdict"].update(allowed=["PASS", "FAIL"]), "verdict.allowed")


def test_missing_agent_markdown(tmp_path):
    project = write_project(tmp_path, demo_config())
    (project / "agents" / "qa.md").unlink()
    with pytest.raises(ConfigError, match="roles.qa"):
        load_config(project)


def test_bad_regex(tmp_path):
    expect_error(tmp_path, lambda d: d.update(approval_commands=["(unclosed"]), "approval_commands[0]")


def test_undefined_placeholder_in_prompt(tmp_path):
    expect_error(tmp_path, lambda d: d["prompts"].update(make="做 {unitt}"), "prompts.make", "unitt")


def test_vars_cannot_use_unit(tmp_path):
    expect_error(tmp_path, lambda d: d.update(vars={"X": "{unit}"}), "vars")


def test_vars_cannot_shadow_builtin(tmp_path):
    expect_error(tmp_path, lambda d: d.update(vars={"WORK": "x"}), "內建佔位符")


def test_unknown_station_kind(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"][4].update(kind="deploy"), "stations[4]", "kind")


def test_commit_path_must_be_relative(tmp_path):
    expect_error(tmp_path, lambda d: d["stations"][4].update(paths=["../x.txt"]), "paths[0]")


def test_render_rules():
    assert render("{a}-{{x}}-${1:-1}", {"a": "1"}) == "1-{x}-${1:-1}"
    assert render("{a}", {"a": "{b}"}) == "{b}"  # 值不會被再次展開
    with pytest.raises(MissingVar):
        render("{zzz}", {})


def test_patterns_overlap():
    assert patterns_overlap("c:/w/**", "c:/w/a.txt")
    assert patterns_overlap("c:/w/a.txt", "c:/w/a.txt")
    assert not patterns_overlap("c:/run/qa/**", "c:/w/a.txt")
    assert patterns_overlap("c:/w/*.sh", "c:/w/**")
