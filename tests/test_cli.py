"""
Unit tests for consequence_gate CLI entry point.
"""

import json
from consequence_gate.cli import default_evaluator, main
import pytest


def test_default_evaluator():
    assert default_evaluator({"tool_name": "delete_user", "tool_args": {}}) == "DENY"
    assert default_evaluator({"tool_name": "drop_table", "tool_args": {}}) == "DENY"
    assert default_evaluator({"tool_name": "transfer_funds", "tool_args": {"amount": 6000}}) == "ASK"
    assert default_evaluator({"tool_name": "transfer_funds", "tool_args": {"amount": 1000}}) == "ALLOW"
    assert default_evaluator({"tool_name": "get_user", "tool_args": {"id": 1}}) == "ALLOW"


def test_cli_backtest(tmp_path, capsys, monkeypatch):
    trace_file = tmp_path / "traces.jsonl"
    traces = [
        {"tool_name": "get_balance", "existing_gate_decision": "ALLOW", "actual_execution_status": "SUCCESS"},
        {"tool_name": "delete_all", "existing_gate_decision": "ALLOW", "actual_execution_status": "FAILED"},
    ]
    with open(trace_file, "w") as f:
        for t in traces:
            f.write(json.dumps(t) + "\n")

    monkeypatch.setattr("sys.argv", ["consequence-gate", "backtest", str(trace_file), "--json"])
    main()
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["total_traces"] == 2
    assert report["false_negative_caught"] == 1
    assert report["true_negative"] == 1
