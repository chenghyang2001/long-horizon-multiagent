"""通知：Telegram Bot API（只用標準庫 urllib，不另裝套件）。

設定（factory.yaml）：
  notify:
    telegram:
      on: [alert, finish]     # alert＝寫進 ALERT（單位 failed／rejected、hours 寫 ALERT.txt）；finish＝hours 結束摘要
金鑰只從環境變數讀：TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID；缺就印警告、不發，引擎照常跑。
保護：token 不出現在任何輸出、log、trace、例外訊息或 ALERT（所有對外文字都先遮蔽 token 與 chat id）；
     代理與指令站的子程序環境不會有這兩個變數（claude_call.base_env 移除）。
逾時 10 秒、失敗重試 1 次；最後仍失敗只印警告，絕不讓通知失敗中斷引擎。
測試：LH_TELEGRAM_API_BASE 可把 API 指到本機假伺服器（只接受 http／https）；假模型模式沒設它時一律不發。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from lhf.config import is_fake, notify_events_raw

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ENV = "TELEGRAM_CHAT_ID"
SECRET_ENVS = (TOKEN_ENV, CHAT_ENV)
API_BASE_ENV = "LH_TELEGRAM_API_BASE"
DEFAULT_API = "https://api.telegram.org"
EVENTS = ("alert", "finish")
MAX_CHARS = 3900  # Telegram 單則上限 4096；留一點給標題與截斷記號
TIMEOUT_SEC = 10
RETRY_WAIT_SEC = 2
MASK = "***"


def enabled_events(cfg):
    telegram = ((cfg.data.get("notify") or {}).get("telegram")) or {}
    return set(notify_events_raw(telegram) or [])


def wants(cfg, event):
    return event in enabled_events(cfg)


def secrets_present():
    return all(os.environ.get(name) for name in SECRET_ENVS)


def missing_env_warning(cfg):
    """啟動時提醒：設定要發 Telegram，但環境變數不齊（不中斷）。"""
    if not enabled_events(cfg) or secrets_present():
        return None
    missing = [name for name in SECRET_ENVS if not os.environ.get(name)]
    return f"notify.telegram 已設定，但缺環境變數 {'、'.join(missing)}，不會發 Telegram 通知"


def redact(text):
    """把 token、chat id 換成 ***（也處理 URL 編碼後的樣子）。"""
    text = str(text)
    for name in SECRET_ENVS:
        value = os.environ.get(name)
        if not value:
            continue
        for form in {value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)}:
            text = text.replace(form, MASK)
    return text


def shorten(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_message(cfg, title, unit="-", reason="", summary=""):
    """專案名＋標題＋單位＋原因＋摘要，總長不超過 MAX_CHARS。"""
    lines = [f"[lhf] {cfg.name}｜{title}"]
    if unit and unit != "-":
        lines.append(f"單位：{unit}")
    if reason:
        lines.append(f"原因：{shorten(reason, 1500)}")
    if summary:
        lines += ["", str(summary)]
    return shorten("\n".join(lines), MAX_CHARS)


def api_base():
    base = (os.environ.get(API_BASE_ENV) or DEFAULT_API).rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ValueError(f"{API_BASE_ENV} 只接受 http:// 或 https:// 開頭")
    return base


def describe_error(exc):
    """只描述錯誤種類與 Telegram 的說明文字；不含 URL（URL 裡有 token）。"""
    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            payload = json.loads(exc.read(4096).decode("utf-8", "replace"))
            detail = f"：{payload.get('description')}" if isinstance(payload, dict) else ""
        except (OSError, ValueError):
            pass
        return f"HTTP {exc.code}{detail}"
    if isinstance(exc, urllib.error.URLError):
        return f"連線失敗：{exc.reason}"
    return f"{type(exc).__name__}：{exc}"


def post_message(text):
    """送一則訊息；成功回 None，失敗回錯誤說明（已遮蔽）。"""
    url = f"{api_base()}/bot{os.environ[TOKEN_ENV]}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": os.environ[CHAT_ENV], "text": text, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            payload = json.loads(response.read(65536).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:  # HTTPError 是 URLError 的子類別
        return redact(describe_error(exc))
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        description = payload.get("description") if isinstance(payload, dict) else payload
        return redact(f"Telegram 回應 ok 不是 true：{shorten(description, 200)}")
    return None


def warn(text):
    print(f"⚠ 通知：{redact(text)}", file=sys.stderr, flush=True)


def send(cfg, event, *, title, unit="-", reason="", summary=""):
    """依設定發一則通知；回傳是否送達。任何失敗都只警告（含意料外的例外），不往外丟。"""
    try:
        if not wants(cfg, event):
            return False
        if not secrets_present():
            warn(missing_env_warning(cfg) or "缺 Telegram 環境變數")
            return False
        if is_fake() and not os.environ.get(API_BASE_ENV):
            print("    （假模型模式：略過真的 Telegram 通知）", flush=True)
            return False
        text = build_message(cfg, title, unit, reason, summary)
        return deliver(text)
    except Exception as exc:  # noqa: BLE001 — 通知是附屬功能，任何錯誤都不可中斷引擎
        warn(f"Telegram 通知發生未預期的錯誤：{describe_error(exc)}")
        return False


def deliver(text):
    error = None
    for attempt in (1, 2):
        error = post_message(text)
        if error is None:
            return True
        if attempt == 1:
            time.sleep(RETRY_WAIT_SEC)
    warn(f"Telegram 通知失敗（已重試 1 次）：{error}")
    return False
