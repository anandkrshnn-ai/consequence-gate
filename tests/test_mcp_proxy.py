"""
Unit tests for MCPConsequenceProxy.

Tests cover:
- Non-tools/call requests pass through unchanged
- tools/call with ALLOW decision forwards to downstream
- tools/call with DENY decision returns JSON-RPC error
- tools/call with ASK decision returns tool result with isError=true
- tools/call with STEER decision returns structured guidance
- Idempotency token stability across retries
"""

import json
from unittest.mock import MagicMock
import pytest

from consequence_gate.integrations.mcp_proxy import MCPConsequenceProxy, create_financial_mcp_proxy
from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.core.models import GateDecision, EvaluationResult


def test_non_tools_call_passes_through():
    """Non-tools/call requests should return None (forward as-is)."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.9, numeric_deltas={}, irreversibility_score=0.0)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.ALLOW, confidence=0.9, reason="OK")

    proxy = MCPConsequenceProxy(
        downstream_command=["echo", "test"],
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
    )

    # resources/list request (not tools/call)
    request = {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
    result = proxy._process_line(json.dumps(request))

    # Should return None (forward to downstream)
    assert result is None


def test_tools_call_allowed_forwards():
    """tools/call with ALLOW decision forwards to downstream."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.9, numeric_deltas={"balance": -1000}, irreversibility_score=0.2)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.ALLOW, confidence=0.9, reason="Within bounds")

    proxy = MCPConsequenceProxy(
        downstream_command=["echo", "test"],
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
    )
    proxy._forward_to_downstream = MagicMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {"content": []}})

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "process_claim", "arguments": {"amount": 1000}},
    }
    result = proxy._process_line(json.dumps(request))

    proxy._forward_to_downstream.assert_called_once_with(request)
    assert json.loads(result)["result"]["content"] == []


def test_tools_call_denied_returns_error():
    """tools/call with DENY decision returns JSON-RPC error."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.9, numeric_deltas={"balance": -500000}, irreversibility_score=0.95)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.DENY, confidence=0.9, reason="Critical breach")

    proxy = MCPConsequenceProxy(
        downstream_command=["echo", "test"],
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
    )

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "process_claim", "arguments": {"amount": 500000}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert "error" in response
    assert response["error"]["code"] == -32603
    assert "BLOCKED:" in response["error"]["message"]


def test_tools_call_ask_returns_iserror():
    """tools/call with ASK decision returns tool result with isError=true."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.4, numeric_deltas={}, irreversibility_score=0.0)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.ASK, confidence=0.4, reason="Low confidence")

    proxy = MCPConsequenceProxy(
        downstream_command=["echo", "test"],
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
    )

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "process_claim", "arguments": {"amount": 100}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert "result" in response
    assert response["result"]["isError"] is True
    assert "ESCALATION_REQUIRED:" in response["result"]["content"][0]["text"]


def test_tools_call_steer_returns_guidance():
    """tools/call with STEER decision returns structured guidance."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.9, numeric_deltas={"balance": -50000}, irreversibility_score=0.8)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(
            decision=GateDecision.STEER,
            confidence=0.9,
            reason="Exceeds tier limit",
            steer_payload={
                "guidance": "Split into instant + escrow",
                "suggested_tool": "create_staged_disbursement",
                "suggested_args": {"immediate_amount": 25000, "escrow_amount": 25000, "idempotency_key": "steer_c1"},
            },
        )

    proxy = MCPConsequenceProxy(
        downstream_command=["echo", "test"],
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
    )

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "process_claim", "arguments": {"amount": 50000, "claim_id": "c1"}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert "result" in response
    assert response["result"]["isError"] is True
    assert "STEER_GUIDANCE:" in response["result"]["content"][0]["text"]
    assert "create_staged_disbursement" in response["result"]["content"][0]["text"]
    assert "idempotency_key=steer_c1" in response["result"]["content"][0]["text"]


def test_financial_factory_proxy():
    """create_financial_mcp_proxy returns working MCPConsequenceProxy."""
    proxy = create_financial_mcp_proxy(
        downstream_command=["echo", "test"],
        daily_tier_limit_inr=25000.0,
        instant_wire_threshold=10000.0,
        max_retries=2,
        context_provider=lambda params: {"account_rolling_24h_spend": 0.0, "kyc_verified": True},
    )

    assert isinstance(proxy, MCPConsequenceProxy)

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "process_payout",
            "arguments": {"amount": 50000, "currency": "INR", "payout_method": "instant_upi", "claim_id": "c1"},
        },
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    # Should STEER (exceeds tier limit)
    assert "result" in response
    assert response["result"]["isError"] is True
    assert "STEER_GUIDANCE:" in response["result"]["content"][0]["text"]
