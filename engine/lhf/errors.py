"""引擎共用的例外類別：集中一處，避免模組互相 import 造成循環。"""


class ConfigError(Exception):
    """factory.yaml 格式或內容不合法；訊息一律以「欄位路徑：原因」開頭。"""


class MissingVar(ConfigError):
    """範本用到未定義的佔位符；gate 用它判斷「需要單位卻沒有單位」而拒絕。"""

    def __init__(self, name, template):
        super().__init__(f"佔位符 {{{name}}} 未定義（範本：{str(template)[:80]!r}）")
        self.name = name


class RoundError(Exception):
    """可預期的本輪錯誤（模型呼叫失敗、檔案缺漏等）；單位維持原站，下次執行重試該站。"""


class CallBudgetExceeded(RoundError):
    """本單位 claude -p 次數超過上限；直接標 failed，避免無止境重試燒額度。"""
