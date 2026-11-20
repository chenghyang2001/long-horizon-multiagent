# 階段 3：加上控制

研究團隊（主持人 host＋研究員 researcher＋審查員 reviewer）每一次工具呼叫前，
Claude Code 的 PreToolUse hook 都會先執行 `gate.py`，決定 allow／deny，並寫進 `trace.jsonl`。
規則全部寫在 `policy.yaml`，對照 K142 HarnessLab 的 IntentVerifier、CapabilityBudget、Tracer。

| 檔案 | 用途 |
| --- | --- |
| `policy.yaml` | 角色合約、預算、需人工核准的動作 |
| `gate.py` | hook 本體＋`--approve`／`--report`／`--reset` |
| `send_report.py` | 模擬寄出 `report.md` 到 `outbox/` |
| `.claude/settings.json` | 註冊 PreToolUse hook（matcher `*`） |
| `.claude/agents/` | researcher、reviewer 子代理定義 |

開始前先 `python gate.py --reset`，在本目錄開 `claude`。

## 3-1 意圖合約

每個角色只能用 `policy.yaml` 裡列出的工具。子代理的呼叫帶有 `agent_type`，主持人沒有（視為 `host`）。
`python send_report.py` 這個 Bash 指令被當成虛擬工具 `send_report`；其他 Bash 一律是 `Bash`，不在任何角色清單裡。

示範：請主持人「自己用 WebSearch 查一下 X」→ 被擋，原因「host 的合約不允許 WebSearch」；
改成「派 researcher 去查」→ 放行。

## 3-2 權限預算

`budgets` 限制同一個 session 內某角色某工具被放行的次數，例如 researcher 的 WebSearch 每輪最多 5 次。
`send_report` 屬於 `escalate`：沒有人工核准就擋下，並記到 `pending-approvals.jsonl`。

示範：

1. 要研究員查 8 個子題 → 第 6 次 WebSearch 起被擋，原因「預算用完」。
2. 要主持人寄出報告 → 被擋，原因「需要人工核准」。
3. 在另一個終端機執行 `python gate.py --approve send_report`，再叫主持人重寄 → 放行。
4. 再寄一次 → 又被擋（核准只能用一次）。

## 3-3 追蹤紀錄

每次決策都附加一行到 `trace.jsonl`（時間、session、角色、工具、決策、原因、輸入前 80 字）。
gate 自己出錯（輸入 JSON 壞掉、policy 讀不到）時一律 deny（fail closed）。

示範：跑完一輪後執行

```bash
python gate.py --report                 # 全部紀錄＋各角色 allow/deny 統計
python gate.py --report --session <ID>  # 只看某一輪
```

不靠 claude 也能測：直接把 hook JSON 餵給 stdin，例如

```bash
echo '{"session_id":"s1","tool_name":"WebSearch","tool_input":{"query":"ROS 2"},"agent_type":"researcher"}' | python gate.py
```

## 注意

- matcher 是 `*`，主持人用 TodoWrite、Bash（非 send_report）等清單外工具也會被擋，這是刻意的。
- hook 指令用 `$CLAUDE_PROJECT_DIR`；Windows 上 Claude Code 透過 Git Bash 執行 hook，變數可正常展開。
