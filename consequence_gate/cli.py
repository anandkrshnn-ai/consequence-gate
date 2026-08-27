"""
consequence-gate CLI: consequence simulation and backtest utilities.

Usage:
    consequence-gate --help
    consequence-gate version
    consequence-gate backtest traces.jsonl
"""

import argparse
import sys
from pathlib import Path

from .backtest.harness import load_traces, run_backtest
from .backtest.reporter import generate_report
from .simulators.financial import FinancialDeltaPredictor
from .core.circuit_breaker import SteerCircuitBreaker
from . import __version__


def simulate_and_evaluate_factory():
    """Create a default simulator + evaluator for backtest."""
    predictor = FinancialDeltaPredictor()
    breaker = SteerCircuitBreaker()

    def fn(trace: dict):
        delta = predictor.simulate(
            trace["tool_name"],
            trace["tool_args"],
            trace.get("session_context", {}),
        )
        result = predictor.evaluate(delta, breaker)
        return result.decision.value

    return fn


def cmd_backtest(args):
    """Run backtest on historical traces."""
    traces_path = Path(args.traces_file)
    if not traces_path.exists():
        print(f"Error: {traces_path} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Loading traces from {traces_path}...")
    traces = load_traces(str(traces_path))
    print(f"Loaded {len(traces)} traces")

    print("Running backtest...")
    simulate_fn = simulate_and_evaluate_factory()
    results = run_backtest(traces, simulate_fn)
    report = generate_report(results)

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
        description="Speculative outcome-simulation gate & trace backtesting for AI agent tool calls",
    )
    parser.add_argument("-v", "--version", action="version", version=f"consequence-gate {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # Backtest subcommand
    backtest_parser = subparsers.add_parser("backtest", help="Run offline backtesting on a JSONL trace file")
    backtest_parser.add_argument("traces_file", help="Path to JSONL traces file")
    backtest_parser.set_defaults(func=cmd_backtest)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
