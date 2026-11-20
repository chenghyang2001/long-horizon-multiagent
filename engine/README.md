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
python -m lhf ../projects/k143 approve ch14            # 核准等待中的單位
python -m lhf ../projects/k143 reset ch14              # 看過 ALERT 後重來
python -m pytest tests -q                              # 假模型模式回歸測試
```

| 子指令 | 說明 |
| --- | --- |
| `run [--unit X] [--allow-project-permissions]` | 跑一輪 |
| `hours --hours N [--max-rounds M] [--gap-seconds S] [--allow-project-permissions]` | 連續跑多輪 |
| `status`／`dry-run [--unit X]` | 唯讀 |
| `init [ids...] [--force]` | 建立 queue（沒給 id＝全部單位） |
| `approve <id>`／`reset <id>`／`reset-calls <id>` | 引擎執行中會排進 `run/commands.jsonl`，下一站開始時套用 |
| `migrate --from <舊 state.json> [--force]` | 第一代 capstone 進度轉移 |

`--allow-project-permissions`：工作目錄的 `.claude/settings*.json` 有 `permissions.allow`、
`bypassPermissions` 或 `acceptEdits` 時，引擎預設拒絕執行；確認無誤後才加這個旗標。

## 結束碼

| 碼 | run | hours |
| --- | --- | --- |
| 0 | 單位完成，或停在等待核准 | 正常停止，且沒有 failed／rejected 單位 |
| 1 | 單位被判 failed／rejected | 有單位被判 failed／rejected（照樣做完其他單位），寫 ALERT.txt |
| 2 | 設定錯誤，拒絕啟動 | 引擎錯誤連發（連續 `max_engine_errors`＋1 輪），或 state.json 損毀；寫 ALERT.txt |
| 3 | 引擎錯誤（模型呼叫失敗、gate 沒生效…），停在原站下次重試 | — |
| 4 | 已有另一個 run／hours 在跑 | 同左 |
| 130 | Ctrl+C（進度已存） | Ctrl+C |

同一單位連續 3 次引擎錯誤（`limits.max_engine_errors`）會標 failed＋ALERT，換下一個單位。

## 安全與穩定

- **鐵律站鏈**：缺 producer／verify／qa／review／finish 任一環就拒絕啟動；撰寫者、QA、審查必須是不同角色；
  QA／審查只能用 Read Glob Grep Write Edit；退回只能回到 producer，REJECT 後一定重跑 verify＋qa。
- **gate**（每次工具呼叫）：全域禁止 → 引擎內部檔保護 → 工具合約 → 寫入路徑 → Bash 白名單（含 `; & | < > `` ` `` $` 與換行）
  → 需核准指令（綁單位＋輪次、用一次失效）→ 預算。內部錯誤一律拒絕。gate 讀本輪開始時的設定快照（含 sha256）。
- **逾時殺整棵程序樹**：Windows 用 Job Object（Git Bash 的背景孫程序 taskkill /T 殺不到），輸出寫暫存檔不用管線。
- **執行鎖** `run/.runtime/run.lock`：PID＋程序啟動時間，殘留舊鎖自動清除。
- **state.json 驗證**：缺欄位、未知狀態、站名不存在都會明確報錯，不覆寫檔案。

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
| run 結束碼 | 0／1／130 | 0／1／2／3／4／130（見上表） |
| hours 停止 | 連續 2 輪非 0 | 只有引擎錯誤連發才停；單位失敗會繼續下一個 |
| commit 前 | 不檢查分支 | 檢查 `branch`，不同就拒絕 |

## 從第一代接手（k143）

```bash
cd ~/workspace/long-horizon-multiagent/engine
PYTHONUTF8=1 python -m lhf ../projects/k143 migrate --from ../capstone/state.json
PYTHONUTF8=1 python -m lhf ../projects/k143 status        # ch04～ch13 done，下一個 ch14
PYTHONUTF8=1 python -m lhf ../projects/k143 run           # 真模型跑 ch14 驗收
```

done／failed／rejected 照搬（輪次、次數、成本、判定紀錄）；做到一半的單位重設為 pending 從頭做。
repo 裡的 `projects/k143/run/state.json` 已是轉移後的結果（2026-11-21），其他電腦 pull 後直接 `run` 即可。
