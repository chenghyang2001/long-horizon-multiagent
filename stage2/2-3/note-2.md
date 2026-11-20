# Claude Code 無頭模式（claude -p / print mode）適合什麼情境？

## 一、claude -p 是什麼
- 在 `claude` 後面加 `-p`（`--print`）就會在沒有互動式介面的情況下執行：給一次 prompt，跑完 agent 迴圈、印出結果就結束。官方文件說這個模式以前叫 headless mode，現在叫「Run Claude Code programmatically」。來源：https://code.claude.com/docs/en/headless
- 官方範例：`claude -p "Find and fix the bug in auth.py" --allowedTools "Read,Edit,Bash"`。來源：https://code.claude.com/docs/en/headless
- 結束代碼：成功回 0，失敗回非 0，腳本可以依這個值決定下一步。來源：https://code.claude.com/docs/en/headless
- `--output-format`：`text`（預設）、`json`（整次執行包成一個結構化結果，可以用 `jq` 取 `.result`、`.structured_output`）、`stream-json`（每行一個 JSON 事件，適合即時監看）。不少社群資料說 `stream-json` 要搭配 `--verbose`；`--include-partial-messages` 可以輸出逐 token 的事件。來源：https://code.claude.com/docs/en/cli-reference 、https://institute.sfeir.com/en/claude-code/claude-code-headless-mode-and-ci-cd/
- `--json-schema`：讓輸出符合指定的 JSON Schema 並通過驗證，只在 print mode 可用，適合下游程式直接解析。來源：https://code.claude.com/docs/en/headless
- `--allowedTools` / `--disallowedTools` / `--permission-mode`：事先核准或禁止工具。沒人能按「允許」時，未核准的工具會被拒絕，不會一直卡住。來源：https://code.claude.com/docs/en/headless
- `--max-turns`（預設不限）與 `--max-budget-usd`：限制回合數和花費，避免失控。來源：https://code.claude.com/docs/en/headless
- `--bare`：不自動載入 hooks、skills、MCP、CLAUDE.md，讓 CI 每次結果一致。官方推薦腳本和 SDK 呼叫用這個模式，未來會變成 `-p` 的預設。來源：https://code.claude.com/docs/en/headless
- 不是 Python／TypeScript 的語言，可以用子行程執行 `claude -p --output-format json`；Python／TS 用 Agent SDK 比較好。來源：https://docs.anthropic.com/en/docs/claude-code/sdk/sdk-headless

## 二、適合的情境
- CI/CD 流水線：自動審查 PR、修 lint、產生 release notes、分析測試失敗原因。GitHub Actions 有官方的 `anthropics/claude-code-action`，可以用 `claude_args` 把參數傳下去。來源：https://angelo-lima.fr/en/claude-code-cicd-headless-en/ 、https://hidekazu-konishi.com/entry/claude_code_cicd_and_headless_automation.html
- 腳本與排程（cron / schtasks）：每天整理 log、產生報告、檢查相依套件。來源：https://eseospace.com/blog/claude-code-headless-mode-automation/
- pre-commit 或 hooks：commit 前自動檢查 diff、產生 commit message。這類工作通常是唯讀，權限風險低。來源：https://joeyyu23.github.io/claude-code-handbook/en/book2-advanced/09-automated-workflows
- 批次處理：用迴圈對很多檔案或很多項目逐一呼叫 `claude -p`（例如大量遷移、翻譯、摘要），各自獨立又可以平行跑。來源：https://code.claude.com/docs/en/best-practices
- pipe 串接：`cat error.log | claude -p "找出根本原因"`，或用 `--output-format json | jq` 把結果交給下一個工具，符合 Unix 哲學。來源：https://code.claude.com/docs/en/cli-reference
- 程式化整合：讓其他程式把 Claude 當子行程呼叫，搭配 `--json-schema` 拿到結構化結果。來源：https://code.claude.com/docs/en/headless

## 三、不適合的情境與注意事項
- 需要來回討論、邊做邊確認的探索式工作（設計討論、需求還不清楚、要人判斷的取捨），比較適合互動模式；`-p` 執行中沒辦法問你。來源：https://code.claude.com/docs/en/best-practices
- 權限：CI runner 沒有 TTY，沒人能按「Allow」。建議優先順序是：精準的 `--allowedTools` → `--permission-mode` → `--dangerously-skip-permissions`。最後一個只能在隔離、用完即丟的容器裡用。來源：https://www.truefoundry.com/blog/claude-code-dangerously-skip-permissions 、https://www.vincentschmalbach.com/how-to-run-claude-code-headless-in-ci-without-hanging-on-permission-prompts/
- GitHub Actions 有兩層權限：workflow YAML 的 `permissions:`（GitHub token 能做什麼），以及 Claude 自己的工具權限，兩層都要收斂到最小。來源：https://claude-code-guide-f3m.pages.dev/github-actions/
- 成本：批次或排程很容易在沒人看的時候燒掉大量 token，一定要設 `--max-turns`、`--max-budget-usd`，並從 json 輸出記錄花費。來源：https://code.claude.com/docs/en/headless
- 靜默失敗：有案例是 exit 0 但 commit 根本不存在，所以要驗證實際產出，不能只看結束代碼。來源：https://wmedia.es/en/tips/claude-code-dangerously-skip-permissions-silent-failure
- 可重現性：本機的 CLAUDE.md、MCP、hooks 會影響結果，CI 建議用 `--bare` 並明確指定設定。機密（API key）用 CI secrets 注入，不要寫在 prompt 或 log 裡。來源：https://code.claude.com/docs/en/headless
- 提示注入風險：在 PR／issue 這類外部內容上自動執行時，要假設輸入可能有惡意指令，所以要限制可寫入、可執行的工具。來源：https://agentpatterns.ai/workflows/headless-claude-ci/
