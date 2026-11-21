# lhf：長時程多代理工廠通用引擎

把「一個工作單位走完 撰寫 → 客觀驗證 → QA → 審查 → 收尾」做成共用引擎；每個專案只寫一份
`projects/<專案>/factory.yaml`＋`agents/*.md`。欄位說明見 `templates/kindle-drill/factory.yaml`。

## 用法

```bash
cd ~/workspace/long-horizon-multiagent/engine
export PYTHONUTF8=1
python -m lhf ../projects/k143 dry-run --unit ch14     # 只印每站要做什麼
python -m lhf ../projects/k143 status
python -m lhf ../projects/k143 run                     # 跑一輪（一個單位）
python -m lhf ../projects/k143 hours --hours 5 --gap-seconds 30
python -m lhf ../projects/k143 pending-info ch14       # 核准前看待核准內容（JSON）
python -m lhf ../projects/k143 approve ch14            # 核准等待中的單位
python -m lhf ../projects/k143 reset ch14              # 看過 ALERT 後重來
python -m lhf ../projects/k143 stop --wait 0           # 請執行中的 run／hours 做完這站後停下（只送要求）
python -m lhf ../projects/k143 ack-control             # 結束碼 6 之後：看控制檔的變動（加 --yes 才解除）
python -m lhf ../projects/k143 verify-commits          # 換電腦後確認 state 記的 commit 都在工作目錄
bash launch-hours.sh ../projects/k143 5                # 跑 hours 並在結束後保留視窗（skill 用 wt 開它）
python -m pytest tests -q                              # 假模型模式回歸測試
```

日常操作建議用 skill **`lh-factory`**（`~/.claude/skills/lh-factory/SKILL.md`）：在控制台說
「開工廠：k143」「工廠狀態」「工廠核准 ch15」「工廠停止」「新工廠專案：<名稱> <類型>」「工廠接續」，
skill 會照步驟呼叫這些子指令（核准一定先在前景用選單問過人才執行 approve）。

| 子指令 | 說明 |
| --- | --- |
| `run [--unit X] [--allow-project-permissions]` | 跑一輪 |
| `hours --hours N [--max-rounds M] [--gap-seconds S] [--allow-project-permissions]` | 連續跑多輪 |
| `status`／`dry-run [--unit X]` | 唯讀 |
| `init [ids...] [--force]` | 建立 queue（沒給 id＝全部單位） |
| `approve <id>`／`reset <id>`／`reset-calls <id>` | 引擎執行中會排進 `run/commands.jsonl`，下一站開始時套用 |
| `pending-info <id> [--diff 檔]` | 唯讀，輸出 JSON：單位狀態、停在哪站、站型或指令核准、待核准紀錄、`workdir_changes`（工作目錄目前的改動＋上游之後的 commit，不一定都是本單位改的）、`unit_commits`／`unit_commit_files`（本單位 commit 站記下的 commit 與其中的檔）、`executable_files`（其中推上去會被執行的檔，含來源）。所有字串的控制字元與雙向控制字元都跳脫。`--diff 檔` 輸出該檔的安全 diff（不執行 repo 設定的 fsmonitor／hook／外部 diff／textconv／filter，控制字元跳脫，最多 400 行） |
| `stop [--now] [--wait 秒] [--force／--no-force]` | 請執行中的 run／hours 收尾停下（見下方「停止」）；預設等 300 秒、**不強制** |
| `ack-control [--yes]` | 結束碼 6 之後：印出鎖存紀錄與控制檔相對於基準的變動；加 `--yes` 才更新基準、解除鎖存。人工指令（三層檢查），引擎在跑時拒絕（4） |
| `verify-commits` | 每個單位 state 記的 commit 都必須存在且在工作目錄 HEAD 的祖先上；缺任何一個結束碼 1 |
| `migrate --from <舊 state.json> [--force]` | 第一代 capstone 進度轉移 |

`--allow-project-permissions`：工作目錄的 `.claude/settings*.json` 有 `permissions.allow`、
`bypassPermissions` 或 `acceptEdits` 時，引擎預設拒絕執行；確認無誤後才加這個旗標。

## 結束碼

| 碼 | run | hours |
| --- | --- | --- |
| 0 | 單位完成，或停在等待核准 | 正常停止，且沒有 failed／rejected 單位 |
| 1 | 單位被判 failed／rejected | 本次有單位新變成 failed／rejected（照樣做完其他單位），寫 ALERT.txt；開跑前就失敗、還沒 reset 的單位只列在摘要，不重發 ALERT |
| 2 | 設定錯誤，拒絕啟動（含 claude 不支援 `--restricted`） | 同左；或引擎錯誤連發（連續 `max_engine_errors`＋1 輪）、state.json 損毀；寫 ALERT.txt |
| 3 | 引擎錯誤（模型呼叫失敗、gate 沒生效…），停在原站下次重試 | — |
| 4 | 已有另一個 run／hours 在跑 | 同左 |
| 5 | approve／reset／reset-calls／init／migrate／stop 從引擎底下的程序發出，拒絕 | — |
| 6 | 工作目錄控制檔被改動（指令站期間，或與持久基準不同），或 `run/CONTROL-CHANGED.json` 鎖存還在：拒絕／停止 | 同左：停止、寫 ALERT.txt，不做下一個單位；之後重開一律拒絕，直到 `ack-control --yes` |
| 130 | Ctrl+C 或 stop（進度已存） | Ctrl+C 或 stop |

其他子指令：`pending-info` 0＝正在等核准、1＝沒有在等核准（照樣輸出 JSON）；
`stop` 0＝已停止（或本來就沒在跑、或 `--wait 0` 只送出要求）、4＝等完 `--wait` 還在收尾（沒加 `--force`，停止要求仍有效）、
1＝`--force` 逾時後已強制結束、3＝強制結束失敗、5＝從引擎底下發出被拒；`ack-control` 0＝完成或只顯示、4＝引擎在跑、5＝被拒；
`verify-commits` 0＝全部在、1＝有缺。

## 結束碼 6 之後怎麼恢復

1. `status` 最上面會有 🔴「工作目錄控制檔被改動（CONTROL-CHANGED.json）」；`cat run/ALERT.txt` 看是哪一輪、哪些檔。
2. `ack-control`（不加 `--yes`）：列出每個變動的檔（新增／刪除／修改、危險 git 設定鍵）。
3. **查清楚是誰改的**：worktree 的共用 config 與 hooks 跟主 repo 共用，可能是人在主 repo 操作（例如裝了 husky）；
   不是人做的就當成被植入，先刪掉／還原那些檔，不要 ack。
4. 確認安全後 `ack-control --yes`：把目前的控制檔存成新基準（`run/control-baseline.json`）、刪除鎖存，才能重開 run／hours。
- 基準跨重開保留，只有 `ack-control --yes` 會更新：直接刪 `CONTROL-CHANGED.json` 沒有用，下一次 run 會比對出同樣的差異再鎖存。
- `control-baseline.json` 每台電腦一份（路徑是本機的，`.gitignore`）；第一次在這台跑時自動建立。
  `CONTROL-CHANGED.json` 進版控：換電腦接手也看得到「上次停在控制檔被改」。兩個檔代理都不可寫（gate 保護清單）。

- 假模式（`LH_FAKE_MODEL=1`）與 `LH_WORK_OVERRIDE` 的工作目錄在別處：用自己的基準 `run/.fake-control-baseline.json`
  與鎖存 `run/.fake-control-changed.json`（不進版控），真跑↔假跑切換不會互相誤鎖存；假模式的工作目錄換了會重建基準。
  真跑的 `workdir` 被改則照樣鎖存（訊息寫「工作目錄換了」）。
- 第一次建立基準時會印出 hook 檔與危險 git 設定鍵給人過目；`stop-budget` 印出 stop 最多要等的秒數（skill 用）。

### 已知限制

- 建立基準之前就存在的 `filter.*.clean`（例如 git LFS）在 commit 站 `git add` 時仍會執行：環境已清乾淨（沒有
  `TELEGRAM_*`、`LH_*`），但不強制關閉 filter，免得破壞 LFS。之後新增或修改 filter 設定屬於危險鍵，會被鎖存。

## 啟動檢查（B 階段）

`run`／`hours` 開跑前執行一次 `claude --help`，沒有 `--restricted` 就印「請升級 claude」並以結束碼 2 拒絕啟動
（沒有它代理會載入使用者全域 CLAUDE.md，見下方「代理不載入任何 CLAUDE.md」）。
結果快取在 `run/.runtime/claude-capability.json`，以執行檔路徑＋大小＋修改時間為鍵：升級後自動重新檢查，平常每輪不重跑。
假模型模式（`LH_FAKE_MODEL=1`）略過。

## 停止（`stop`）

hours 跑在另一個 Windows Terminal 視窗（另一個主控台），從別的終端送 CTRL_BREAK 送不到
（`GenerateConsoleCtrlEvent` 只能送給同一個主控台；AttachConsole 換主控台會連 claude 一起打斷、也會讓呼叫端失去主控台）。
所以改用檔案 `run/.runtime/stop-request.json`：

| 指令 | 行為 |
| --- | --- |
| `stop` | run 每站開始前看到就停；hours 本輪結束或輪間等待時看到就停。目前這站一定做完、存檔 |
| `stop --now` | hours 對**自己的**子程序送 CTRL_BREAK（同一個主控台，送得到）：目前這站中斷、下次重做這站 |
| `--wait 秒`（預設 300） | 等持鎖程序結束；等不到就回結束碼 4（**預設不強制**：agent 站一站可能十幾分鐘，強制會丟掉整站進度與額度） |
| `--force` | 等完 `--wait` 還沒停才 `taskkill /T /F` 持鎖程序整棵樹（Job Object 帶走所有子孫；不用 `/IM`，不會殺到別的程序），殘留的鎖與標記下次啟動自動清除 |
| `--wait 0` | 只送出要求就回來（之後用 `status` 輪詢；skill 用這個） |

單獨執行的 `run`（不是 hours 底下）沒有人替它送 CTRL_BREAK：`--now` 與一般 `stop` 一樣，都是做完目前這站才停。

hours 以 130 結束、摘要的停止原因是「收到停止要求（stop）」。停止要求檔不列入指令站前後比對（人可能在指令站執行中下 stop）。

## 通知（`notify`）

```yaml
notify:
  telegram:
    on: [alert, finish]   # alert：單位寫進 ALERT 清單時、hours 寫 ALERT.txt 時；finish：hours 寫結束摘要時
```

- 金鑰只走環境變數 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`；缺了啟動時警告、照常執行，不發通知。
- 只用標準庫 urllib；逾時 10 秒、失敗重試 1 次，最後仍失敗只印警告，絕不中斷引擎。
- token 與 chat id 不會出現在任何輸出、log、trace、例外訊息或 ALERT（對外文字一律遮蔽）；代理與指令站的子程序環境
  也拿不到這兩個變數。
- 訊息含專案名、標題、單位、原因、簡短摘要，超過 3900 字截斷（Telegram 上限 4096）。
- Gmail／LINE 由 skill 在前景結束時發（skill 有 MCP），引擎不碰。
- 測試：`LH_TELEGRAM_API_BASE` 可指到本機假伺服器；假模型模式沒設它時一律不發真通知。

同一單位連續 3 次引擎錯誤（`limits.max_engine_errors`）會標 failed＋ALERT，換下一個單位。

## 安全與穩定

- **鐵律站鏈**：缺 producer／verify／qa／review／finish 任一環就拒絕啟動；撰寫者、QA、審查必須是不同角色；
  QA／審查只能用 Read Glob Grep Write Edit；退回只能回到 producer，REJECT 後一定重跑 verify＋qa。
- **gate**（每次工具呼叫）：全域禁止 → 引擎內部檔保護 → 工具合約 → 寫入路徑 → Bash 白名單（含 `; & | < > `` ` `` $` 與換行）
  → 需核准指令（綁單位＋輪次、用一次失效）→ 預算。內部錯誤一律拒絕。gate 讀本輪開始時的設定快照（含 sha256）。
- **逾時殺整棵程序樹**：Windows 用 Job Object（Git Bash 的背景孫程序 taskkill /T 殺不到），輸出寫暫存檔不用管線。
- **執行鎖** `run/.runtime/run.lock`：PID＋程序啟動時間，殘留舊鎖用互斥檔安全清除；鎖 token 只交給 hours 自己啟動的
  run 子程序，代理與指令站拿不到（它們執行 approve／reset 只會排進佇列）。
- **代理不載入任何 CLAUDE.md**：claude -p 一律加 `--restricted --strict-mcp-config`。2026-11-21 以 claude 2.1.329
  實測，光靠 `--setting-sources project,local` 仍會載入使用者全域 `~/.claude/CLAUDE.md`（個人規則會讓代理自己
  commit／push、破壞 §9 角色分離）；`--restricted` 下全域與專案 CLAUDE.md 都不載入，`--settings` 的 gate hook
  照樣觸發、deny 生效，`--tools` 指名的 Bash／WebFetch 仍可用，Max 訂閱登入正常。不可改用 `--bare`
  （只吃 API key）或 `--safe-mode`（停用 hook）。
- **commit 站預設不跑 git hook（B 階段）**：commit 站的 git 一律帶 `-c core.hooksPath=<空裝置> -c core.fsmonitor=false`；
  factory.yaml 的 commit 站設 `hooks: true` 才放行，此時 `.husky/`、`.githooks/`、`.lefthook/`、`.pre-commit-config.yaml`、
  `lefthook.yml` 與 `core.hooksPath` 指的資料夾都列入控制檔比對（被改就停）。引擎跑的所有 git（commit 站、控制檔比對、
  pending-info）都用清過的環境：沒有 `TELEGRAM_*`、`LH_*`（含 `LH_LOCK_TOKEN`）、`LHF_*`、API key，只保留
  `LHF_IN_ENGINE=1`——hook 若想執行 `lhf approve` 會被第一層擋下，這個旗標本身不給任何權限。
- **控制檔被改就停（B 階段）**：指令站前後比對到工作目錄控制檔變動 → run 結束碼 6；hours 開跑時拍一份控制檔基準、
  每輪開始前比對，有變動或 run 回 6 都停止並寫 ALERT.txt，不再做下一個單位（下一個 commit 站會執行被植入的 hook）。
  基準是持久的 `run/control-baseline.json`（不是每次開跑重拍），變動會寫鎖存 `run/CONTROL-CHANGED.json`，見「結束碼 6 之後怎麼恢復」。
  git 設定（`.git/config`、worktree 的共用 config、`config.worktree`，含 include 的檔）**只比對危險鍵**：`core.hooksPath`、
  `core.fsmonitor`、`core.sshCommand`、`core.editor`／`pager`、`*.helper`、`include*`／`includeIf*`、`filter.*`、`alias.*`、
  `credential.*`、`http.*`、`url.*`、`remote.*.url`、`diff.*.textconv`、`merge.*.driver` 等（清單見 `integrity.DANGEROUS_KEY_RES`），
  由 git 自己解析（解析失敗改整檔比對）；`hooks/` 仍整個比對。worktree 的共用 config 與 hooks 跟主 repo 共用，人在主 repo
  fetch、建分支、設上游不會誤判；真的改到危險鍵或 hook 時訊息會註明「可能是人在主 repo 操作」。
- **CLAUDE.md 掃描**略過 `build`／`install`／`log`／`logs`／`dist`／`target`／`node_modules`／`.venv`／`venv`／`__pycache__`／
  `.next`／`.cache` 等大目錄（`integrity.SKIP_DIRS`），最多走訪 20,000 個資料夾，超過就警告並停止掃描。
- **殘留的指令站標記**：標記記下自己的 PID＋啟動時間與當時的持鎖 PID；人工指令只認「標記程序與持鎖程序都還活著、且持鎖者
  相符」的有效標記，當掉留下的不會讓人白等。run 與 hours 每輪開始時清掉不是自己放的標記。
- **佇列互斥檔** `run/.runtime/queue.mutex`：引擎在互斥檔內放指令站標記，人工指令在互斥檔內確認沒有有效標記才排入，
  所以排入的指令不會落在站前快照與站後快照之間（不會被誤判竄改、也不會被還原掉）。排入後再確認一次指令還在
  （佇列 → 認領檔 → state 的 applied_commands），不見了就用**同一個 id** 重排；引擎依 id 只套用一次，不會重複套用。
- **工作目錄控制檔**：workdir 與 agent_cwd 之下，路徑任何一段是 `.git`（worktree 的 gitdir 檔也算）、`.gitmodules`、
  `.gitattributes`、`.claude`、`.mcp.json`，或檔名是 `CLAUDE.md`／`CLAUDE.local.md`（任何子目錄、不分大小寫），
  gate 一律拒絕寫入。比對前照 Windows 語意去掉每段結尾的「.」與空白、`::$DATA` 串流。CLAUDE.md 只能由人更新。
- **指令站前後比對**（verify 站沙箱完成前的過渡措施）：指令站執行前後比對佇列、核准、state、設定快照、
  factory.yaml、agents、工作目錄控制檔與 CLAUDE*.md；有變動 → 該單位標 failed＋ALERT，引擎檔還原
  （本站期間新增的佇列指令與核准作廢）。人下的 approve／reset 會等指令站跑完才排入，不會被誤判。
- **人工指令只能由人下**：approve／reset／reset-calls／init／migrate 三層檢查——引擎子程序都帶 `LHF_IN_ENGINE=1`（快速擋下）；Windows 上持鎖程序建立具名 Job，引擎所有子孫（含 start /b、Start-Process 的孤兒）都在 job 裡，
  `IsProcessInJob` 為真就拒絕；再加祖先程序鏈檢查（持鎖的引擎 PID 在祖先鏈上就拒絕）。POSIX 沒有 job 這層。佇列裡沒有提交者檢查紀錄的指令不套用並發 ALERT。限制：指令站跑的撰寫者腳本目前沒有沙箱，理論上能直接改 run/ 底下的檔案（verify 站沙箱排在後續階段）。
- **指令佇列** `run/commands.jsonl`：每筆有 id，套用結果存進 state.json 後才刪認領檔，當機後重讀也不會重複套用。
- **子程序環境**：claude -p 只拿到 gate 需要的 `LH_PROJECT_DIR／LH_UNIT／LH_ROUND／LH_ROLE／LH_STATION／LH_TRACE_OFFSET`；
  指令站一個 `LH_*` 都沒有。gate 也拒絕 Bash 指令呼叫引擎本身（`-m lhf`、`lhf/`、引擎路徑）。
- **工作目錄控制檔**：workdir 與 agent_cwd 底下的 `.claude/`、`.mcp.json`、`.git/` 一律不可寫；
  每次 claude -p 前都檢查 `.claude/settings*.json` 有沒有放寬權限。
- **state.json 驗證**：缺欄位、未知狀態、站名不存在都會明確報錯，不覆寫檔案。

## 版控策略（run/ 底下哪些進 git）

| 進版控（跨電腦接手需要） | 不進版控（.gitignore） |
| --- | --- |
| `state.json`（進度）、`trace.jsonl`（gate 紀錄）、`alerts.jsonl`（ALERT 清單） | `.runtime/`（鎖、hook 設定、設定快照、假模式標記、停止要求、佇列互斥檔、claude 能力快取） |
| `plans/`、`reviews/`、`test-logs/`、`logs/hours-*`（各站產物與連續執行摘要） | `heartbeat.txt`、`commands*.jsonl`（只在本機執行中有意義） |
| `approvals/`（核准紀錄） | `logs/agent-fail-*`（完整 stdout／stderr，可能很大）、`trace-*.jsonl`（歸檔） |

換電腦接手前先確認舊電腦沒有在跑（`status` 不會顯示「執行中」），再 commit／pull。

## 與第一代（capstone/）的差異

| 項目 | 第一代 | 新引擎 |
| --- | --- | --- |
| 狀態檔 | `capstone/state.json`，`chapters` | `projects/<專案>/run/state.json`，`units`（每單位多 `station`、`retries`、`once_done`、`approved`、`engine_errors`） |
| 狀態值 | planned／written／ran／judged／reviewed… | pending／in_progress／pending_approval／done／failed／rejected＋`station` |
| 環境變數 | `LH_CHAPTER` | `LH_UNIT`，另加 `LH_PROJECT_DIR`、`LH_STATION`、`LH_TRACE_OFFSET` |
| trace 欄位 | `chapter` | `unit`；另有 role=engine 的 verdict／rework 紀錄 |
| 設定 | `policy.yaml`＋程式內常數 | `factory.yaml`（`schema: 1`） |
| 指令站非 0 | 交給 tester 判 | 引擎直接判 FAIL（`allow_nonzero` 例外）；k143 已核對結果相同 |
| 退回意見 | 只附失敗報告 | 失敗報告＋raw log＋（審查退回時）最近一次 QA 報告 |
| 代理上下文 | `--setting-sources project,local`（仍會載入全域 CLAUDE.md） | `--restricted`：不載入任何 CLAUDE.md（含全域）、只有 `--tools` 指名的工具 |
| run 結束碼 | 0／1／130 | 0／1／2／3／4／130（見上表） |
| hours 停止 | 連續 2 輪非 0 | 只有引擎錯誤連發才停；單位失敗會繼續下一個 |
| commit 前 | 不檢查分支 | 檢查 `branch`，不同就拒絕 |
| 通知 | 無 | Telegram（`notify.telegram.on`，B 階段） |
| 停止 | 只能 Ctrl+C | `stop`（檔案要求，逾時才強制結束）；B 階段 |

## B 階段與 A 階段刻意不同的行為

| 項目 | A 階段 | B 階段 |
| --- | --- | --- |
| 指令站期間工作目錄控制檔被改 | 單位 failed，run 結束碼 1，hours 繼續下一個單位 | run 結束碼 6，hours 停止＋ALERT＋鎖存；重開拒絕直到 ack-control |
| commit 站的 git hook | 會跑，git 繼承完整環境 | 預設不跑（`hooks: true` 才跑）；git 用清過的環境 |
| `stop` | — | 預設不強制，`--force` 才強制 |
| git config 比對 | 整檔雜湊 | 只比對危險鍵（含 include 的檔）；解析失敗才整檔 |
| `notify` 欄位 | 只檢查是物件，啟動時警告「尚未實作」 | 驗證 `{telegram: {on: [...]}}`，舊的 `{on_alert: ...}` 會被拒絕（結束碼 2） |
| run／hours 啟動 | 不檢查 claude 版本 | `claude --help` 沒有 `--restricted` 就拒絕（結束碼 2） |
| 人工指令遇到指令站標記 | 標記檔存在＋鎖活著就等 | 只等有效標記；排入與開站用互斥檔序列化 |
| state 單位欄位 | — | 多 `commits`（commit 站記下的 commit；舊 state 沒有也能讀） |

## 從第一代接手（k143）

```bash
cd ~/workspace/long-horizon-multiagent/engine
PYTHONUTF8=1 python -m lhf ../projects/k143 migrate --from ../capstone/state.json
PYTHONUTF8=1 python -m lhf ../projects/k143 status        # ch04～ch13 done，下一個 ch14
PYTHONUTF8=1 python -m lhf ../projects/k143 run           # 真模型跑 ch14 驗收
```

done／failed／rejected 照搬（輪次、次數、成本、判定紀錄）；做到一半的單位重設為 pending 從頭做。
repo 裡的 `projects/k143/run/state.json` 已是轉移後的結果（2026-11-21），其他電腦 pull 後直接 `run` 即可。
