"""
CLI entry point for consequence-gate.
"""

import argparse
import json
import sys
from consequence_gate import __version__
from consequence_gate.backtest.harness import load_traces, run_backtest
from consequence_gate.backtest.reporter import generate_report


def default_evaluator(trace: dict) -> str:
    """Heuristic evaluator for CLI demo and trace analysis."""
    tool = trace.get("tool_name", "")
    args = trace.get("tool_args", {})

    tool_lower = tool.lower()
    args_str = str(args).lower()

    if any(k in tool_lower for k in ("delete", "drop", "purge", "truncate")) or any(
        k in args_str for k in ("drop ", "delete from", "truncate ", "purge")
    ):
        return "DENY"
    if "transfer" in tool.lower() or "pay" in tool.lower():
        amount = args.get("amount", 0)
        if isinstance(amount, (int, float)) and amount > 5000:
            return "ASK"
    return "ALLOW"


def main():
    parser = argparse.ArgumentParser(
        prog="consequence-gate",
        description="Speculative outcome-simulation gate & trace backtesting for AI agent tool calls.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Backtest subcommand
    bt_parser = subparsers.add_parser("backtest", help="Run offline backtesting on a JSONL trace file")
    bt_parser.add_argument("file", help="Path to JSONL file containing recorded agent traces")
    bt_parser.add_argument("--json", action="store_true", help="Output results in JSON format")

    args = parser.parse_args()

    if args.command == "backtest":
        try:
            traces = load_traces(args.file)
            results = run_backtest(traces, default_evaluator)
            report = generate_report(results)

            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print("\n================ CONSEQUENCE GATE BACKTEST REPORT ================")
                print(f"Total Traces Evaluated:      {report['total_traces']}")
                print(f"True Negatives:              {report['true_negative']}")
                print(f"False Negatives Caught:      {report['false_negative_caught']}")
                print(f"False Positives Relieved:    {report['false_positive_relieved']}")
                print(f"Other / Unclassified:        {report['other']}")
                print("-----------------------------------------------------------------")
                print(f"False Negative Catch Rate:   {report['false_negative_rate']:.2%}")
                print(f"False Positive Relief Rate:  {report['false_positive_relief_rate']:.2%}")
                print("=================================================================\n")
        except Exception as e:
            print(f"Error executing backtest: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
