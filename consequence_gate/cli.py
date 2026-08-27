"""
consequence-gate CLI: consequence simulation and backtest utilities for AI agent tool calls.

Usage:
    consequence-gate --help
    consequence-gate --version
    consequence-gate backtest traces.jsonl [--json]
"""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .backtest.harness import load_traces, run_backtest
from .backtest.reporter import generate_report
from .core.circuit_breaker import SteerCircuitBreaker
from .simulators.financial import FinancialDeltaPredictor


def default_evaluator(trace: dict) -> str:
    """Heuristic multi-domain evaluator for CLI backtesting across financial, DB, and comms tools."""
    tool = trace.get("tool_name", "")
    args = trace.get("tool_args", {})
    ctx = trace.get("session_context", {})

    tool_lower = tool.lower()
    args_str = str(args).lower()

    # 1. Database blast-radius hazards
    if any(k in tool_lower for k in ("delete", "drop", "purge", "truncate")) or any(
        k in args_str for k in ("drop ", "delete from", "truncate ", "purge")
    ):
        return "DENY"

    # 2. Financial velocity & high-value escalations
    if "transfer" in tool_lower or "pay" in tool_lower or "payout" in tool_lower or "refund" in tool_lower:
        amount = args.get("amount", 0)
        spend = ctx.get("account_rolling_24h_spend", 0)
        tier_limit = ctx.get("tier_limit", 25000)
        if isinstance(amount, (int, float)):
            if spend + amount > tier_limit:
                return "DENY"
            if amount > 5000:
                return "ASK"

    # 3. Communications broadcast & suppression hazards
    if any(k in tool_lower for k in ("broadcast", "campaign", "blast", "newsletter", "email", "sms", "notify")):
        recipients = args.get("recipient_count") or args.get("recipients_count") or 0
        if recipients > 10000 or ctx.get("suppression_verified") is False:
            return "DENY"

    # 4. Unknown/ambiguous tools
    if "unknown" in tool_lower or not tool:
        return "ASK"

    return "ALLOW"



def cmd_backtest(args):
    """Run backtest on historical traces."""
    traces_path = Path(args.traces_file)
    if not traces_path.exists():
        print(f"Error: {traces_path} not found", file=sys.stderr)
        sys.exit(1)

    try:
        traces = load_traces(str(traces_path))
    except Exception as e:
        print(f"Error loading traces: {e}", file=sys.stderr)
        sys.exit(1)

    results = run_backtest(traces, default_evaluator)
    report = generate_report(results)

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
    else:
        print()
        print("================ CONSEQUENCE GATE BACKTEST REPORT ================")
        print(f"Total Traces Evaluated:      {report['total_traces']}")
        print(f"Benign Pass-Through (TN):    {report['true_negative']}")
        print(f"Hazards Intercepted:         {report['false_negative_caught']}")
        print(f"Over-Blocked Relieved (FP):  {report['false_positive_relieved']}")
        print(f"Ambiguous / Escalated:       {report['other']}")
        print("-" * 66)
        print(f"Hazard Interception Share:   {report['false_negative_rate']:.2%}")
        print(f"False Positive Relief Share: {report['false_positive_relief_rate']:.2%}")
        print("=" * 66)


def main():
    parser = argparse.ArgumentParser(
        prog="consequence-gate",
        description="Speculative outcome-simulation gate & trace backtesting for AI agent tool calls.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Backtest subcommand
    bt_parser = subparsers.add_parser("backtest", help="Run offline backtesting on a JSONL trace file")
    bt_parser.add_argument("traces_file", help="Path to JSONL traces file")
    bt_parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    bt_parser.set_defaults(func=cmd_backtest)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
