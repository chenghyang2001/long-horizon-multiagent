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
引擎執行中時 approve／reset／reset-calls 會排進 run/commands.jsonl，由引擎下一站開始時套用。
結束碼見 engine/README.md。執行前記得 PYTHONUTF8=1，並把 engine/ 加進 PYTHONPATH（或在 engine/ 底下執行）。
"""

import argparse
import os
import signal
import sys

from lhf import hours, legacy
from lhf import round as round_mod
from lhf.claude_call import ALLOW_PROJECT_PERMISSIONS_ENV
from lhf.common import EXIT_CONFIG, EXIT_LOCKED, EXIT_REFUSED
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
    }
    return commands[args.command]()


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
    install_break_handler()
    if getattr(args, "allow_project_permissions", False):
        os.environ[ALLOW_PROJECT_PERMISSIONS_ENV] = "1"  # hours 的每輪子程序也會繼承
    try:
        cfg = load_config(args.project_dir)
    except ConfigError as exc:
        print(f"設定錯誤（拒絕啟動）：{exc}", file=sys.stderr)
        return EXIT_CONFIG
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
