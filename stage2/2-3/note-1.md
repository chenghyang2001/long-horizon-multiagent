# Claude Code 的子代理（subagent）適合什麼情境？

查詢日期：2026-11-20。官方文件以 code.claude.com/docs/en/sub-agents 為主（docs.anthropic.com／docs.claude.com 舊網址會導向這裡）；第三方部落格的數字只能當估計。

## 一、子代理是什麼
- 子代理是專門處理特定任務的助手。每個子代理有**自己的 context window**、自訂的 system prompt、指定可用的工具，權限也各自獨立；只在單一 session 內運作。來源：https://code.claude.com/docs/en/sub-agents
- 定義方式：一個 Markdown 檔，開頭放 YAML frontmatter。frontmatter 欄位有 `name`、`description`、`tools`、`disallowedTools`、`model`、`permissionMode` 等，必填的只有 name 和 description，內文就是 system prompt。來源：https://code.claude.com/docs/en/sub-agents 、https://alexcloudstar.com/blog/claude-code-subagents-guide-2026/
- 放置位置：專案層放 `.claude/agents/`，使用者層放 `~/.claude/agents/`，兩處都會遞迴掃描子資料夾。名稱相同時，優先順序是 managed > 專案 > 使用者。官方建議把專案層的定義放進版本控制，讓團隊共用。來源：https://code.claude.com/docs/en/sub-agents
- 主代理會依照 `description` 的內容主動把任務分派出去，所以 description 要寫清楚什麼時候該用這個子代理。管理介面用 `/agents`，命令列可以用 `claude agents` 列出所有子代理。如果 `~/.claude/agents/` 是在 session 進行中才建立的，可能要重開 session 才會載入。來源：https://code.claude.com/docs/en/sub-agents
- model 可以每個子代理分開設定，例如簡單工作交給較便宜的模型。也可以用環境變數 `CLAUDE_CODE_SUBAGENT_MODEL` 統一指定；沒設定時沿用主對話的模型。各來源寫的解析順序不一致，要以官方頁為準。內建的 Explore 子代理只有唯讀工具。來源：https://code.claude.com/docs/en/sub-agents 、https://glukhov.org/ai-devtools/claude-code/claude-code-subagents/

## 二、適合的情境
- **保護主 context（最主要的好處）**：探索過程中雜亂的中間結果都留在子代理自己的視窗，主對話只拿到最後的結論，不會被雜訊塞滿。來源：https://claude.com/blog/subagents-in-claude-code 、https://dev.to/diggidydale/escaping-the-dumbzone-part-2-subagents-divide-and-conquer-1p29
- **大量搜尋只要結論**：例如在程式碼庫裡探索、追查 bug 根因、讀很多檔案。有一個經驗法則是：如果調查會讓主 context 多出大約 1,000 tokens 以上，就考慮交給子代理。來源：https://hackernoon.com/navigating-claude-code-subagents-done-right
- **平行獨立工作**：彼此不依賴的任務（例如同時研究多個子題、分別查不同模組）可以同時派好幾個子代理跑。來源：https://code.claude.com/docs/en/sub-agents 、https://claude-code-guide-f3m.pages.dev/sub-agents.md
- **專業分工**：code review、測試、除錯、資料分析這類工作，可以各給一套專用 prompt，並限制工具（例如 reviewer 只給唯讀權限）。讓「寫程式的 session」和「審查的子代理」分開，審查會比較客觀。來源：https://code.claude.com/docs/en/sub-agents 、https://aicrossroads.substack.com/p/claude-code-subagents
- **安全與權限隔離**：用 `tools`／`disallowedTools` 限制子代理能做什麼，降低誤刪檔、誤改檔的風險。來源：https://code.claude.com/docs/en/sub-agents

## 三、不適合的情境
- **很小的查詢**：子代理一啟動就有固定開銷（載入 system prompt、工具定義等）。有第三方估計大約 7.5k～44k tokens，數字不一，只能參考。查一個檔案、改一行這種事直接在主對話做比較划算。來源：https://glukhov.org/ai-devtools/claude-code/claude-code-subagents/
- **需要來回溝通、緊密耦合的工作**：子代理看不到主對話的歷史，資訊要靠任務說明一次交代清楚。步驟彼此高度依賴、或需要使用者中途介入的工作，不適合拆出去。來源：https://www.ksred.com/claude-code-agents-and-subagents-what-they-actually-unlock/
- **只是想固定一條規則或流程**：這種需求用 CLAUDE.md 規則、hook 或 skill 就夠了，不必動用子代理。來源：https://dev.to/rulestack/claude-code-subagents-when-to-delegate-and-when-a-rule-or-hook-is-enough-ajn

## 四、成本與注意事項
- 子代理回傳的內容還是會進主 context。如果子代理回一份很長的報告，隔離就省不到 tokens，所以要求它只回精簡結論。來源：https://glukhov.org/ai-devtools/claude-code/claude-code-subagents/
- 平行派出 N 個子代理，總 token 消耗大約是 N 倍，換到的是時間；另外還要多等待和整合結果。可以讓簡單的子任務改用較便宜的模型（例如 Haiku）來降低成本。來源：https://hackernoon.com/navigating-claude-code-subagents-done-right
- 如果用 fork 的方式讓子代理繼承母對話，可以共用 prompt cache、比較便宜，但會失去 context 隔離。來源：https://claude-world-com.pages.dev/tutorials/s04-subagents-and-context-isolation
- 判斷原則：只有當「隔離 context、平行加速、專業分工」帶來的好處大於「多花的 tokens 和等待時間」時，才派子代理。來源：https://knightli.com/en/2026/07/10/claude-code-subagent-project-fit-guide/
