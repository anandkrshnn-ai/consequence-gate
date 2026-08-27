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
from .simulators.communications import CommunicationsDeltaPredictor
from .simulators.database import DatabaseDeltaPredictor
from .simulators.financial import FinancialDeltaPredictor


def create_default_evaluator():
    """Create a multi-domain evaluator (financial, database, comms) with heuristic fallback."""
    financial = FinancialDeltaPredictor()
    database = DatabaseDeltaPredictor()
    comms = CommunicationsDeltaPredictor()
    breaker = SteerCircuitBreaker()

    def evaluate_trace(trace: dict) -> str:
        tool = trace.get("tool_name", "")
        args = trace.get("tool_args", {})
        context = trace.get("session_context", {})

        tool_lower = tool.lower()
        args_str = str(args).lower()

        try:
            # Route to appropriate domain simulator if applicable
            if any(k in tool_lower for k in ("transfer", "pay", "charge", "refund", "payout", "spend")):
                delta = financial.simulate(tool, args, context)
                return financial.evaluate(delta, breaker).decision.value
            if any(k in tool_lower for k in ("sql", "db", "query", "database", "table", "record", "migrate")):
                delta = database.simulate(tool, args, context)
                return database.evaluate(delta, breaker).decision.value
            if any(k in tool_lower for k in ("email", "slack", "sms", "notify", "broadcast", "publish", "message")):
                delta = comms.simulate(tool, args, context)
                return comms.evaluate(delta, breaker).decision.value
        except Exception:
            pass

        # Heuristic fallback for other tools
        if any(k in tool_lower for k in ("delete", "drop", "purge", "truncate")) or any(
            k in args_str for k in ("drop ", "delete from", "truncate ", "purge")
        ):
            return "DENY"
        return "ALLOW"

    return evaluate_trace


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

    evaluator = create_default_evaluator()
    results = run_backtest(traces, evaluator)
    report = generate_report(results)

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
    else:
        print()
        print("================ CONSEQUENCE GATE BACKTEST REPORT ================")
        print(f"Total Traces Evaluated:      {report['total_traces']}")
        print(f"True Negatives:              {report['true_negative']}")
        print(f"False Negatives Caught:      {report['false_negative_caught']}")
        print(f"False Positives Relieved:    {report['false_positive_relieved']}")
        print(f"Other / Unclassified:        {report['other']}")
        print("-" * 66)
        print(f"False Negative Catch Rate:   {report['false_negative_rate']:.2%}")
        print(f"False Positive Relief Rate:  {report['false_positive_relief_rate']:.2%}")
        print("=" * 66)


def main():
    parser = argparse.ArgumentParser(
        prog="consequence-gate",
        description="Speculative outcome-simulation gate & trace backtesting for AI agent tool calls.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"consequence-gate {__version__}")
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
