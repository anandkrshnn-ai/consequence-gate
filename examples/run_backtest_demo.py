"""
Minimal runnable demo of the backtest harness against the sample traces.
"""

from consequence_gate.backtest.harness import load_traces, run_backtest
from consequence_gate.backtest.reporter import generate_report
from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.simulators.financial import FinancialDeltaPredictor


def simulate_and_evaluate(trace):
    predictor = FinancialDeltaPredictor()
    breaker = SteerCircuitBreaker()
    delta = predictor.simulate(trace["tool_name"], trace["tool_args"], trace["session_context"])
    result = predictor.evaluate(delta, breaker)
    return result.decision.value


if __name__ == "__main__":
    traces = load_traces("backtest_sample_traces.jsonl")
    results = run_backtest(traces, simulate_and_evaluate)
    report = generate_report(results)
    print(report)
