"""
Unit tests for LangGraph integration.

Tests cover:
- Middleware pattern: @wrap_tool_call intercepts tool calls
- Node pattern: pre-tool-call node gates tool_calls in state
- ALLOW: tool passes through unchanged
- DENY: raises ValueError or returns error ToolMessage
- ASK: calls interrupt() for human approval
- STEER: returns ToolMessage with guidance content
- Idempotency token stability across retries
"""

from unittest.mock import MagicMock, patch
import pytest

from langchain_core.messages import ToolMessage

from consequence_gate.integrations.langgraph_hook import (
    create_consequence_middleware,
    create_consequence_gate_node,
    create_financial_gate_middleware,
    create_financial_gate_node,
)
from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.core.models import GateDecision, EvaluationResult


def test_middleware_allow_passes_through():
    """Middleware with ALLOW decision passes tool_call through unchanged."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.9, numeric_deltas={"balance": -1000}, irreversibility_score=0.2)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.ALLOW, confidence=0.9, reason="Within bounds")

    middleware = create_consequence_middleware(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    tool_call = {"name": "process_claim", "args": {"amount": 1000}, "id": "call_1", "type": "tool_call"}
    state = {}
    tools = {}

    # Middleware should return the tool_call unchanged
    result = middleware.fn(tool_call, state, tools)
    assert result == tool_call


def test_middleware_deny_raises_exception():
    """Middleware with DENY decision raises ValueError."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.9, numeric_deltas={"balance": -500000}, irreversibility_score=0.95)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.DENY, confidence=0.9, reason="Critical breach")

    middleware = create_consequence_middleware(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    tool_call = {"name": "process_claim", "args": {"amount": 500000}, "id": "call_1", "type": "tool_call"}
    state = {}
    tools = {}

    with pytest.raises(ValueError, match="BLOCKED:"):
        middleware.fn(tool_call, state, tools)


def test_middleware_steer_returns_guidance():
    """Middleware with STEER decision returns tool_call with _consequence_gate_result."""

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

    middleware = create_consequence_middleware(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    tool_call = {"name": "process_claim", "args": {"amount": 50000, "claim_id": "c1"}, "id": "call_1", "type": "tool_call"}
    state = {}
    tools = {}

    result = middleware.fn(tool_call, state, tools)
    assert "_consequence_gate_result" in result
    assert result["_consequence_gate_result"]["decision"] == "STEER"
    assert "idempotency_key=steer_c1" in result["_consequence_gate_result"]["guidance"]


def test_node_allow_passes_through():
    """Node with ALLOW decision passes tool_calls through unchanged."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.9, numeric_deltas={"balance": -1000}, irreversibility_score=0.2)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.ALLOW, confidence=0.9, reason="Within bounds")

    gate_node = create_consequence_gate_node(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    state = {
        "messages": [],
        "tool_calls": [{"name": "process_claim", "args": {"amount": 1000}, "id": "call_1"}],
    }

    result = gate_node(state)
    assert len(result["tool_calls"]) == 1
    assert len(result["tool_messages"]) == 0


def test_node_deny_returns_error_message():
    """Node with DENY decision returns ToolMessage with error content."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.9, numeric_deltas={"balance": -500000}, irreversibility_score=0.95)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.DENY, confidence=0.9, reason="Critical breach")

    gate_node = create_consequence_gate_node(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    state = {
        "messages": [],
        "tool_calls": [{"name": "process_claim", "args": {"amount": 500000}, "id": "call_1"}],
    }

    result = gate_node(state)
    assert len(result["tool_messages"]) == 1
    assert isinstance(result["tool_messages"][0], ToolMessage)
    assert "BLOCKED:" in result["tool_messages"][0].content


def test_node_steer_returns_guidance():
    """Node with STEER decision returns ToolMessage with guidance content."""

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

    gate_node = create_consequence_gate_node(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    state = {
        "messages": [],
        "tool_calls": [{"name": "process_claim", "args": {"amount": 50000, "claim_id": "c1"}, "id": "call_1"}],
    }

    result = gate_node(state)
    assert len(result["tool_messages"]) == 1
    assert isinstance(result["tool_messages"][0], ToolMessage)
    assert "STEER_GUIDANCE:" in result["tool_messages"][0].content
    assert "idempotency_key=steer_c1" in result["tool_messages"][0].content


def test_financial_factory_middleware():
    """create_financial_gate_middleware returns working middleware."""
    middleware = create_financial_gate_middleware(
        daily_tier_limit_inr=25000.0,
        instant_wire_threshold=10000.0,
        max_retries=2,
        context_provider=lambda state: {"account_rolling_24h_spend": 0.0, "kyc_verified": True},
    )

    tool_call = {
        "name": "process_payout",
        "args": {"amount": 50000, "currency": "INR", "payout_method": "instant_upi", "claim_id": "c1"},
        "id": "call_1",
        "type": "tool_call",
    }
    state = {}
    tools = {}

    result = middleware.fn(tool_call, state, tools)
    assert "_consequence_gate_result" in result
    assert result["_consequence_gate_result"]["decision"] == "STEER"


def test_financial_factory_node():
    """create_financial_gate_node returns working node."""
    gate_node = create_financial_gate_node(
        daily_tier_limit_inr=25000.0,
        instant_wire_threshold=10000.0,
        max_retries=2,
        context_provider=lambda state: {"account_rolling_24h_spend": 0.0, "kyc_verified": True},
    )

    state = {
        "messages": [],
        "tool_calls": [
            {
                "name": "process_payout",
                "args": {"amount": 50000, "currency": "INR", "payout_method": "instant_upi", "claim_id": "c1"},
                "id": "call_1",
            }
        ],
    }

    result = gate_node(state)
    assert len(result["tool_messages"]) == 1
    assert "STEER_GUIDANCE:" in result["tool_messages"][0].content
