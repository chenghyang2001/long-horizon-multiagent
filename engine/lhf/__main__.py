"""命令列入口：python -m lhf <project_dir> <子指令> …

子指令：
  run [--unit X]                          跑一輪（queue 中第一個可執行的單位，或指定單位）
  status                                  印出狀態，不呼叫模型
  init [ids...] [--force]                 建立／重設 queue（沒給 id＝factory.yaml 全部單位）
  dry-run [--unit X]                      印出每站要執行的指令與 prompt 摘要，不執行
  reset-calls <id>                        把該單位 claude -p 次數歸零（修好環境後重跑用）
  reset <id>                              人看過 ALERT 後，把單位整個重設回起點
  approve <id>                            核准等待中的單位，下次 run 重跑該站
  hours --hours N [--max-rounds M] [--gap-seconds S]   連續跑多輪
執行前記得 PYTHONUTF8=1，並把 engine/ 加進 PYTHONPATH（或在 engine/ 底下執行）。
"""

import argparse
import sys

from lhf import hours
from lhf import round as round_mod
from lhf.config import ID_RE, load_config
from lhf.errors import ConfigError, RoundError


def unit_id(text):
    if not ID_RE.match(text):
        raise argparse.ArgumentTypeError(f"單位 id 只能用英數字、底線、連字號，收到 {text!r}")
    return text


def add_hours_args(sub):
    sub.add_argument("--hours", type=float, required=True, help="最多跑幾小時（到時不砍正在跑的輪）")
    sub.add_argument("--max-rounds", type=int, default=None, help="最多跑幾輪")
    sub.add_argument("--gap-seconds", type=float, default=30, help="每輪之間等幾秒（預設 30）")


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m lhf", description="長時程多代理工廠引擎")
    parser.add_argument("project_dir", help="含 factory.yaml 的專案資料夾")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "dry-run"):
        subs.add_parser(name).add_argument("--unit", type=unit_id, help="指定單位")
    subs.add_parser("status")
    init = subs.add_parser("init")
    init.add_argument("ids", nargs="*", type=unit_id)
    init.add_argument("--force", action="store_true", help="重設已有單位的狀態")
    for name in ("reset-calls", "reset", "approve"):
        subs.add_parser(name).add_argument("id", type=unit_id)
    add_hours_args(subs.add_parser("hours"))
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
        "hours": lambda: hours.cmd_hours(cfg, args),
    }
    return commands[args.command]()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    check_hours_args(parser, args)
    try:
        cfg = load_config(args.project_dir)
    except ConfigError as exc:
        print(f"設定錯誤（拒絕啟動）：{exc}", file=sys.stderr)
        return 2
    try:
        return dispatch(cfg, args)
    except (RoundError, ConfigError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"檔案操作錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
