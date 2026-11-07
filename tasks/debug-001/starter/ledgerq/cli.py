"""命令行入口。"""

import argparse
import json
import sys

from .dates import parse_date
from .errors import LedgerError
from .pipeline import load_rows, summarize
from .report import export_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledgerq", description="台账汇总小工具")
    sub = parser.add_subparsers(dest="command", required=True)

    summary = sub.add_parser("summary", help="生成月度汇总 JSON")
    summary.add_argument("--input", required=True)
    summary.add_argument("--output", required=True)
    summary.add_argument("--start")
    summary.add_argument("--end")

    export = sub.add_parser("export", help="导出规范化 CSV")
    export.add_argument("--input", required=True)
    export.add_argument("--output", required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rows = load_rows(args.input)
        if args.command == "summary":
            start = parse_date(args.start) if args.start else None
            end = parse_date(args.end) if args.end else None
            data = summarize(rows, start=start, end=end)
            with open(args.output, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        else:
            export_csv(rows, args.output)
    except LedgerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0
