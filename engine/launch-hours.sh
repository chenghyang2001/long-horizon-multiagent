#!/bin/bash
set -euo pipefail
# 在獨立的 Windows Terminal 分頁跑 lhf hours（skill lh-factory 用）。
# 用法：launch-hours.sh <專案目錄> <時數> [hours 的其他參數，例如 --gap-seconds 30]
# 為什麼要一支腳本：wt.exe → bash -lc "…" 要疊三層引號，還要把分號寫成 \;，很容易壞；
# 改成 wt 只傳「腳本路徑＋參數」，所有邏輯都在這裡。
# 跑完不關視窗（exec bash -l），使用者可以往上捲看完整輸出與結束碼。

usage() {
  echo "用法：$(basename "$0") <專案目錄> <時數> [hours 的其他參數]" >&2
  exit 2
}

[ "$#" -ge 2 ] || usage
project="$1"
hours="$2"
shift 2

# 時數只接受正數（整數或小數），避免把奇怪的字串交給 python 才報錯
if ! [[ "$hours" =~ ^[0-9]+([.][0-9]+)?$ ]] || [[ "$hours" =~ ^0+([.]0+)?$ ]]; then
  echo "錯誤：時數必須是大於 0 的數字，收到「$hours」" >&2
  usage
fi
[ -f "$project/factory.yaml" ] || { echo "錯誤：$project 底下沒有 factory.yaml" >&2; exit 2; }

engine_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$engine_dir" || { echo "錯誤：進不了引擎目錄 $engine_dir" >&2; exit 2; }
export PYTHONUTF8=1

echo "工廠開始：$project｜$hours 小時｜$(date '+%Y-%m-%d %H:%M:%S')"
set +e
python -m lhf "$project" hours --hours "$hours" "$@"
code=$?
set -e
echo
echo "hours 結束碼 $code（$(date '+%Y-%m-%d %H:%M:%S')）；結束碼說明見 engine/README.md。視窗保留，可往上捲看輸出。"
exec bash -l
