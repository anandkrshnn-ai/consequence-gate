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
    print("=== Backtest Report ===")
    print(f"Total traces: {report['total_traces']}")
    print(f"True negatives: {report['true_negative']}")
    print(f"False negatives caught: {report['false_negative_caught']}")
    print(f"False positives relieved: {report['false_positive_relieved']}")
    print(f"False negative rate: {report['false_negative_rate']:.2%}")
    print(f"False positive relief rate: {report['false_positive_relief_rate']:.2%}")


def cmd_version(args):
    """Print version."""
    from . import __version__
    print(f"consequence-gate {__version__}")


def main():
    parser = argparse.ArgumentParser(
        prog="consequence-gate",
        description="Speculative outcome-simulation layer for AI agent tool calls",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Backtest subcommand
    backtest_parser = subparsers.add_parser("backtest", help="Run backtest on historical traces")
    backtest_parser.add_argument("traces_file", help="Path to JSONL traces file")
    backtest_parser.set_defaults(func=cmd_backtest)

    # Version subcommand
    version_parser = subparsers.add_parser("version", help="Print version")
    version_parser.set_defaults(func=cmd_version)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
