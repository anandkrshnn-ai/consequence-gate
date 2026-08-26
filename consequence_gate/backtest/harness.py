"""
Offline backtest harness: replays historical JSONL tool-call traces
through a simulator + evaluator, WITHOUT re-executing anything, to
measure the four-quadrant FP/FN/TN/steer-recovery breakdown against
the trace's recorded existing_gate_decision and actual_execution_status.
"""

import json
from typing import Callable, Dict, Iterable, List


def load_traces(path: str) -> List[Dict]:
    traces = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                traces.append(json.loads(line))
    return traces


def run_backtest(traces: Iterable[Dict], simulate_and_evaluate: Callable[[Dict], str]) -> List[Dict]:
    """
    simulate_and_evaluate: function(trace) -> decision string ("ALLOW"/"DENY"/"ASK"/"STEER")
    Returns per-trace records annotated with quadrant classification.
    """
    results = []
    for trace in traces:
        new_decision = simulate_and_evaluate(trace)
        old_decision = trace.get("existing_gate_decision", "ALLOW")
        outcome = trace.get("actual_execution_status", "UNKNOWN")

        if old_decision == "ALLOW" and new_decision in ("DENY", "STEER", "ASK") and outcome != "SUCCESS":
            quadrant = "FALSE_NEGATIVE_CAUGHT"
        elif old_decision in ("DENY", "ASK") and new_decision == "ALLOW":
            quadrant = "FALSE_POSITIVE_RELIEVED"
        elif old_decision == "ALLOW" and new_decision == "ALLOW":
            quadrant = "TRUE_NEGATIVE"
        else:
            quadrant = "OTHER"

        results.append({**trace, "simulated_decision": new_decision, "quadrant": quadrant})
    return results
