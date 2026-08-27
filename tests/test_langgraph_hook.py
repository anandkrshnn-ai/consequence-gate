"""
Unit tests for LangGraph middleware integration.

Tests cover:
- ALLOW: tool executes normally via handler(request)
- DENY: raises ValueError with "BLOCKED:" message
- ASK: raises ValueError with "ESCALATION_REQUIRED:" message
- STEER: returns ToolMessage with guidance content
- Idempotency token in guidance message
"""

from unittest.mock import MagicMock
import pytest

from langchain_core.messages import ToolMessage

from consequence_gate.integrations.langgraph_hook import (
    create_consequence_middleware,
    create_financial_gate_middleware,
)
from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.core.models import GateDecision, EvaluationResult


class MockToolCallRequest:
    """Mock ToolCallRequest for testing."""
    def __init__(self, tool_call: dict, state: dict = None):
        self.tool_call = tool_call
        self.state = state or {}


def mock_handler(request):
    """Mock handler that returns a successful tool result."""
    return {"result": "success", "tool_call": request.tool_call}


def test_middleware_allow_passes_through():
    """Middleware with ALLOW decision calls handler(request)."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.9, numeric_deltas={"balance": -1000}, irreversibility_score=0.2)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.ALLOW, confidence=0.9, reason="Within bounds")

    middleware = create_consequence_middleware(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    request = MockToolCallRequest({
        "name": "process_claim",
        "args": {"amount": 1000},
        "id": "call_1",
    })

    result = middleware.fn(request, mock_handler)
    assert result["result"] == "success"


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

    request = MockToolCallRequest({
        "name": "process_claim",
        "args": {"amount": 500000},
        "id": "call_1",
    })

    with pytest.raises(ValueError, match="BLOCKED:"):
        middleware.fn(request, mock_handler)


def test_middleware_ask_raises_exception():
    """Middleware with ASK decision raises ValueError with escalation message."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.4, numeric_deltas={}, irreversibility_score=0.0)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.ASK, confidence=0.4, reason="Low confidence")

    middleware = create_consequence_middleware(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    request = MockToolCallRequest({
        "name": "process_claim",
        "args": {"amount": 100},
        "id": "call_1",
    })

    with pytest.raises(ValueError, match="ESCALATION_REQUIRED:"):
        middleware.fn(request, mock_handler)


def test_middleware_steer_returns_tool_message():
    """Middleware with STEER decision returns ToolMessage with guidance."""

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

    request = MockToolCallRequest({
        "name": "process_claim",
        "args": {"amount": 50000, "claim_id": "c1"},
        "id": "call_1",
    })

    result = middleware.fn(request, mock_handler)
    assert isinstance(result, ToolMessage)
    assert "STEER_GUIDANCE:" in result.content
    assert "create_staged_disbursement" in result.content
    assert "idempotency_key=steer_c1" in result.content


def test_financial_factory_middleware():
    """create_financial_gate_middleware returns working middleware."""
    middleware = create_financial_gate_middleware(
        daily_tier_limit_inr=25000.0,
        instant_wire_threshold=10000.0,
        max_retries=2,
        context_provider=lambda state: {"account_rolling_24h_spend": 0.0, "kyc_verified": True},
    )

    request = MockToolCallRequest({
        "name": "process_payout",
        "args": {"amount": 50000, "currency": "INR", "payout_method": "instant_upi", "claim_id": "c1"},
        "id": "call_1",
    })

    result = middleware.fn(request, mock_handler)
    assert isinstance(result, ToolMessage)
    assert "STEER_GUIDANCE:" in result.content
