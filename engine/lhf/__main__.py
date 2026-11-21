"""命令列入口：python -m lhf <project_dir> <子指令> …

子指令：
  run [--unit X] [--allow-project-permissions]   跑一輪（queue 中第一個可執行的單位，或指定單位）
  status                                  印出狀態，不呼叫模型
  init [ids...] [--force]                 建立／重設 queue（沒給 id＝factory.yaml 全部單位）
  dry-run [--unit X]                      印出每站要執行的指令與 prompt 摘要，不執行
  reset-calls <id>                        把該單位 claude -p 次數歸零（修好環境後重跑用）
  reset <id>                              人看過 ALERT 後，把單位整個重設回起點
  approve <id>                            核准等待中的單位，下次 run 重跑該站
  migrate --from <舊 state.json> [--force]  把第一代主控程式的進度轉成本引擎格式
  hours --hours N [--max-rounds M] [--gap-seconds S] [--allow-project-permissions]   連續跑多輪
  stop [--now] [--wait 秒] [--force]       請執行中的 run／hours 收尾停止（--now：中斷目前這站）；加 --force 逾時才強制結束
  pending-info <id> [--diff 檔]           輸出待核准內容（JSON）；--diff 輸出該檔的安全 diff（不執行 repo 的外部程式）
  ack-control [--yes]                     看工作目錄控制檔的變動；--yes 確認安全後解除鎖存、更新基準
  verify-commits                          換電腦後確認 state 記的 commit 都在工作目錄目前的分支上
  stop-budget                             印出「一站最長要多久」的秒數（skill 等 stop 的總上限）
引擎執行中時 approve／reset／reset-calls 會排進 run/commands.jsonl，由引擎下一站開始時套用。
結束碼見 engine/README.md。執行前記得 PYTHONUTF8=1，並把 engine/ 加進 PYTHONPATH（或在 engine/ 底下執行）。
"""

import argparse
import os
import signal
import sys

from lhf import claude_call, hours, integrity, legacy, pending, stop
from lhf import round as round_mod
from lhf.claude_call import ALLOW_PROJECT_PERMISSIONS_ENV
from lhf.common import EXIT_CONFIG, EXIT_CONTROL_CHANGED, EXIT_LOCKED, EXIT_REFUSED
from lhf.config import ID_RE, load_config
from lhf.errors import ConfigError, EngineDescendant, RoundError
from lhf.lock import LockBusy


def unit_id(text):
    if not ID_RE.match(text):
        raise argparse.ArgumentTypeError(f"單位 id 只能用英數字、底線、連字號，收到 {text!r}")
    return text


def add_run_args(sub):
    sub.add_argument(
        "--allow-project-permissions",
        action="store_true",
        help="工作目錄的 .claude/settings*.json 放寬權限時仍執行（確認過才用）",
    )


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m lhf", description="長時程多代理工廠引擎")
    parser.add_argument("project_dir", help="含 factory.yaml 的專案資料夾")
    subs = parser.add_subparsers(dest="command", required=True)
    run = subs.add_parser("run")
    run.add_argument("--unit", type=unit_id, help="指定單位")
    add_run_args(run)
    subs.add_parser("dry-run").add_argument("--unit", type=unit_id, help="指定單位")
    subs.add_parser("status")
    init = subs.add_parser("init")
    init.add_argument("ids", nargs="*", type=unit_id)
    init.add_argument("--force", action="store_true", help="重設已有單位的狀態")
    for name in ("reset-calls", "reset", "approve"):
        subs.add_parser(name).add_argument("id", type=unit_id)
    info = subs.add_parser("pending-info")
    info.add_argument("id", type=unit_id)
    info.add_argument("--diff", metavar="檔", help="輸出這個檔（工作目錄底下的相對路徑）的安全 diff")
    subs.add_parser("ack-control").add_argument("--yes", action="store_true", help="確認變動安全，解除鎖存並更新基準")
    subs.add_parser("verify-commits")
    subs.add_parser("stop-budget")
    stop_parser = subs.add_parser("stop")
    stop_parser.add_argument("--now", action="store_true", help="中斷目前這站（下次重做這站），不等它做完")
    stop_parser.add_argument("--wait", type=float, default=300, help="最多等幾秒讓它收尾（預設 300；0＝只送出要求）")
    force = stop_parser.add_mutually_exclusive_group()
    force.add_argument("--force", dest="force", action="store_true", help="等完 --wait 還沒停就強制結束整棵程序樹")
    force.add_argument("--no-force", dest="force", action="store_false", help="不強制（預設）")
    migrate = subs.add_parser("migrate")
    migrate.add_argument("--from", dest="source", required=True, help="舊版 state.json 路徑")
    migrate.add_argument("--force", action="store_true", help="覆蓋已存在的 run/state.json")
    hours_parser = subs.add_parser("hours")
    hours_parser.add_argument("--hours", type=float, required=True, help="最多跑幾小時（到時不砍正在跑的輪）")
    hours_parser.add_argument("--max-rounds", type=int, default=None, help="最多跑幾輪")
    hours_parser.add_argument("--gap-seconds", type=float, default=30, help="每輪之間等幾秒（預設 30）")
    add_run_args(hours_parser)
    return parser


def check_hours_args(parser, args):
    if args.command != "hours":
        return
    if args.hours <= 0:
        parser.error("--hours 要大於 0")
    if args.max_rounds is not None and args.max_rounds < 1:
        parser.error("--max-rounds 要 ≥ 1")
    if args.gap_seconds < 0:
        parser.error("--gap-seconds 不可為負")


def check_stop_args(parser, args):
    if args.command == "stop" and args.wait < 0:
        parser.error("--wait 不可為負")


def dispatch(cfg, args):
    commands = {
        "run": lambda: round_mod.cmd_run(cfg, args.unit),
        "status": lambda: round_mod.cmd_status(cfg),
        "init": lambda: round_mod.cmd_init(cfg, args.ids, args.force),
        "dry-run": lambda: round_mod.cmd_dry_run(cfg, args.unit),
        "reset-calls": lambda: round_mod.cmd_reset_calls(cfg, args.id),
        "reset": lambda: round_mod.cmd_reset(cfg, args.id),
        "approve": lambda: round_mod.cmd_approve(cfg, args.id),
        "migrate": lambda: legacy.cmd_migrate(cfg, args.source, args.force),
        "hours": lambda: hours.cmd_hours(cfg, args),
        "stop": lambda: guarded_stop(cfg, args),
        "pending-info": lambda: pending.cmd_pending_info(cfg, args.id, args.diff),
        "ack-control": lambda: round_mod.cmd_ack_control(cfg, args.yes),
        "verify-commits": lambda: pending.cmd_verify_commits(cfg),
        "stop-budget": lambda: stop.cmd_stop_budget(cfg),
    }
    return commands[args.command]()


def guarded_stop(cfg, args):
    """stop 也是人工指令：引擎底下的程序（撰寫者腳本、代理）不可叫停工廠。"""
    round_mod.guard_human(cfg, "stop")
    return stop.cmd_stop(cfg, now=args.now, wait=args.wait, force=args.force)


class ControlLatched(Exception):
    """run/CONTROL-CHANGED.json 存在：上次停在「工作目錄控制檔被改動」，要人 ack-control 後才能再跑。"""


def preflight(cfg, args):
    """run／hours 開跑前：控制檔鎖存（結束碼 6）→ claude 必須支援 --restricted（假模型模式略過；有快取）。"""
    if args.command not in ("run", "hours"):
        return
    latch = integrity.read_latch(cfg)
    if latch is not None:
        raise ControlLatched(integrity.latch_summary(latch))
    claude_call.ensure_restricted_support(cfg)


def install_break_handler():
    """hours 用 CTRL_BREAK 請子程序收尾；Python 預設遇到 SIGBREAK 直接結束、不存檔，改成當 Ctrl+C 處理。"""
    if os.name != "nt":
        return

    def handler(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGBREAK, handler)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    check_hours_args(parser, args)
    check_stop_args(parser, args)
    install_break_handler()
    if getattr(args, "allow_project_permissions", False):
        os.environ[ALLOW_PROJECT_PERMISSIONS_ENV] = "1"  # hours 的每輪子程序也會繼承
    try:
        cfg = load_config(args.project_dir)
        preflight(cfg, args)
    except ConfigError as exc:
        print(f"設定錯誤（拒絕啟動）：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except ControlLatched as exc:
        print(
            f"拒絕啟動：工作目錄控制檔被改動（run/{integrity.latch_path(cfg).name}）：{exc}\n"
            "查清楚是誰改的、確認安全後執行 ack-control（看差異）再 ack-control --yes",
            file=sys.stderr,
        )
        return EXIT_CONTROL_CHANGED
    for warning in cfg.startup_warnings():
        print(f"⚠ 警告：{warning}", file=sys.stderr)
    return run_dispatch(cfg, args)


def run_dispatch(cfg, args):
    try:
        return dispatch(cfg, args)
    except LockBusy as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return EXIT_LOCKED
    except EngineDescendant as exc:
        print(f"拒絕：{exc}；請從獨立的終端機執行", file=sys.stderr)
        return EXIT_REFUSED
    except (RoundError, ConfigError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"檔案操作錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
