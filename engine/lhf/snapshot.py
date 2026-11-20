"""設定快照：每輪開始把 factory.yaml 內容＋sha256 存進 run/.runtime/factory.snapshot.json。

gate 只讀快照、不讀即時的 factory.yaml：代理（或任何人）在本輪中途改設定，影響不到正在執行的把關。
快照的 sha256 對不上（被竄改或寫壞）時 gate 直接 fail closed。
"""

import hashlib
import json
from pathlib import Path

from lhf.common import atomic_write_text, now_str
from lhf.config import load_config
from lhf.errors import ConfigError

SNAPSHOT_NAME = "factory.snapshot.json"


def snapshot_path(project_dir):
    return Path(project_dir) / "run" / ".runtime" / SNAPSHOT_NAME


def write_snapshot(cfg):
    record = {"sha256": cfg.sha256, "taken": now_str(), "text": cfg.raw_text}
    atomic_write_text(snapshot_path(cfg.project_dir), json.dumps(record, ensure_ascii=False))


def load_for_gate(project_dir):
    """有快照就用快照（驗 sha256）；沒有快照（尚未跑過任何一輪）才讀即時檔。"""
    path = snapshot_path(project_dir)
    if not path.exists():
        return load_config(project_dir)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        text = record["text"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError(f"設定快照讀取失敗（{path}）：{exc}") from exc
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != record.get("sha256"):
        raise ConfigError(f"設定快照 sha256 不符（{path}），可能被竄改，拒絕使用")
    return load_config(project_dir, text=text)
