# 畢業專案「K143 演練備課工廠」介面合約（給所有實作者）

設計來源：`chenghyang2001/console` 的 `drills/long-horizon-multiagent/capstone-design.md`。本檔是**實作層的約定**，`run_round.py` 與 `gate.py` 都照這份寫。

## 路徑（程式內一律用 `Path.home()` 推算，不硬編碼使用者名稱）

| 名稱 | 路徑 |
| --- | --- |
| `CAPSTONE` | `~/workspace/long-horizon-multiagent/capstone`（＝ `Path(__file__).parent`） |
| `WORKTREE` | `~/workspace/kindle-143-fable5-agentic-ai-factory`（K143 repo 的 worktree，分支 `factory/drills`） |
| `PRACTICE` | `WORKTREE / "practice"` |
| `NOW_TABLE` | `~/workspace/console/drills/k143-interactive/fable-5-to-5.2.md`（只讀） |

## 一輪＝一章，固定 6 站（每站做完立刻寫 `state.json`，可中斷接續）

| 站 | 角色 `LH_ROLE` | 誰執行 | 讀 | 寫 |
| --- | --- | --- | --- | --- |
| 1 plan | `planner` | `claude -p` | `PRACTICE/prompts/chNN.md`（注意：prompts 在 WORKTREE 根目錄 `WORKTREE/prompts/chNN.md`）、`PRACTICE/fable5/**`、`PRACTICE/interactive/ch01.sh`、`ch03.sh`（格式範本） | `CAPSTONE/plans/chNN.md` |
| 2a write | `writer` | `claude -p` | 規劃、範本、（重做時）測試／審查意見 | `PRACTICE/interactive/chNN.sh`、`PRACTICE/interactive/coach-chNN.txt` |
| 2b now | `now-updater` | `claude -p`（與 2a **平行**） | `NOW_TABLE`、`WORKTREE/prompts/chNN.md` | `PRACTICE/interactive/now-chNN.txt` |
| 3 run | （無模型） | `run_round.py` 自己跑 | — | `CAPSTONE/test-logs/chNN-raw.log`：`bash -n chNN.sh`，再 `bash chNN.sh 1..8` 各一次（每步逾時 180 秒，cwd＝`PRACTICE/interactive`） |
| 4 judge | `tester` | `claude -p` | 規劃、raw log | `CAPSTONE/test-logs/chNN.md`，最後一行必須是 `VERDICT: PASS` 或 `VERDICT: FAIL` |
| 5 review | `reviewer` | `claude -p` | 規劃、腳本、教練稿、現況、test log、`WORKTREE/prompts/chNN.md` | `CAPSTONE/reviews/chNN.md`，最後一行 `VERDICT: PASS` 或 `VERDICT: REJECT` |
| 6 commit | （無模型） | `run_round.py` | — | 在 WORKTREE `git add` 三個 chNN 檔＋`git commit`（**不 push**） |

- judge 為 FAIL → 回到 2a（writer 帶著 test log 修），同一輪最多重寫 2 次；仍 FAIL → 本章標 `failed`，結束本輪。
- review 為 REJECT → 回到 2a（帶 review 意見）一次，再 3→4→5；仍 REJECT → 標 `rejected`。

## 呼叫 `claude -p` 的固定方式（全部集中在 `run_round.py` 的 `call_agent(role, prompt)`）

```
claude -p <prompt>
  --append-system-prompt-file <CAPSTONE/agents/<role>.md 的絕對路徑>   # 不可把多行內容放命令列：npm 版 claude.cmd 會在換行處截斷後面所有參數（2026-11-20 i5 踩到，gate 因此沒掛上）
  --tools <該角色工具，見 policy.yaml roles.<role>.tools>
  --settings <CAPSTONE/.runtime/hook-settings.json>   # run_round.py 執行時產生，內含 gate.py 的絕對路徑
  --setting-sources project,local                      # 不載入使用者層級的 hook／CLAUDE.md 設定
  --add-dir <CAPSTONE> --add-dir <NOW_TABLE 所在資料夾>
  --output-format json
cwd = PRACTICE
env：複製 os.environ，移除 ANTHROPIC_API_KEY 與 CLAUDECODE、CLAUDE_CODE_ENTRYPOINT 等巢狀 session 變數；
     加 LH_ROLE=<role>、LH_CHAPTER=chNN、LH_ROUND=<輪次>、PYTHONUTF8=1、K143_CLI_MAX_CALLS=6；
     ORIGINAL_PATH 前面加上 python 執行檔所在資料夾（否則 Claude 的 Bash 工具找不到 python）
```

`hook-settings.json` 內容（run_round.py 產生，路徑用 `as_posix()`）：

```json
{"hooks": {
  "PreToolUse":  [{"matcher": "*", "hooks": [{"type": "command", "command": "\"<python 絕對路徑>\" \"<CAPSTONE>/gate.py\""}]}],
  "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "\"<python 絕對路徑>\" \"<CAPSTONE>/gate.py\" --post"}]}]
}}
```

## gate.py（hook）

- 角色＝環境變數 `LH_ROLE`；沒有就用 hook 輸入的 `agent_type`；都沒有＝`unknown`（一律 deny）。
- 判斷順序：全域禁止 → 意圖合約（工具）→ 路徑合約（Write／Edit 的 `file_path`）→ Bash 白名單 → 預算 → allow。
- 全域禁止（任何角色）：寫入路徑含 `practice/fable5`（大小寫、`\`／`/` 都要正規化）；Bash 指令含 `rm -rf`、`git push`、`.claude/`、`ANTHROPIC_API_KEY`。
- `--post` 模式：讀 PostToolUse 輸入，把「工具實際結果」（成功／失敗、輸出前 120 字）寫一行到 trace，`kind: "result"`。
- trace 每行：`ts`、`round`、`chapter`、`role`、`tool`、`kind`（`decision`／`result`）、`decision`、`reason`、`input`（前 120 字）。
- 任何內部錯誤：fail closed（deny），exit 0。

## 狀態檔 `CAPSTONE/state.json`

```json
{"round": 3,
 "queue": ["ch04", "ch05", "..."],
 "chapters": {"ch04": {"status": "pending|planned|written|ran|judged|reviewed|done|failed|rejected",
                       "rewrites": 0, "review_retries": 0, "verdicts": [], "calls": 0,
                       "started": "...", "finished": "..."}},
 "history": [{"ts": "...", "round": 3, "chapter": "ch04", "event": "..."}]}
```

每輪結束（成功或失敗）都覆寫 `CAPSTONE/heartbeat.txt`：一行 `時間｜第幾輪｜chNN｜成功或失敗｜本輪 claude -p 次數｜耗時`。
