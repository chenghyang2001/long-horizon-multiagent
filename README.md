# long-horizon-multiagent

長時程多代理實作學習計畫的練習 repo（計畫與進度在 `chenghyang2001/console` 的 `drills/long-horizon-multiagent/`）。

- `stage2/`：階段 2「動手蓋」（對照 K138）：長任務代理、平行子代理、研究團隊、中斷與接續、分工修程式
- 呼叫模型一律用 `claude -p`（Max 訂閱），**不設 `ANTHROPIC_API_KEY`**，不跑本機模型

## 通用引擎（2026-11-21 起）

`capstone/` 的 K143 備課工廠已抽成通用引擎 `engine/lhf/`，每個專案只寫 `projects/<專案>/factory.yaml`＋`agents/*.md`。
用法、結束碼、與第一代的差異、接手步驟見 [`engine/README.md`](engine/README.md)；新專案從 `templates/` 複製。
`capstone/` 保留不動，只當舊版參考。

**日常操作用 skill `lh-factory`**（`~/.claude/skills/lh-factory/SKILL.md`，跟著 `~/.claude` 同步到每台 PC）：
在控制台說「開工廠：k143」「工廠跑 5 小時：k143」「工廠狀態」「工廠核准 ch15」「工廠停止」「新工廠專案：<名稱> <類型>」「工廠接續」。
