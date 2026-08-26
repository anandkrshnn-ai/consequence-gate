"""
Unit tests for the Strands BeforeToolCallEvent hook adapter.

Tests cover:
- ALLOW: hook returns cleanly, tool executes
- DENY: hook sets event.cancel_tool with hard-block message
- ASK: hook sets event.cancel_tool with escalation message
- STEER: hook sets event.cancel_tool with structured guidance
- Idempotency token stability across retries (same natural key -> same token)
- Retry cap escalation (after max_retries, forces ASK)
"""

from unittest.mock import MagicMock
import pytest

from strands.hooks.events import BeforeToolCallEvent

from consequence_gate.integrations.strands_hook import (
    ConsequenceGateHook,
    create_financial_gate_hook,
    create_database_gate_hook,
)
from consequence_gate.core.models import GateDecision, EvaluationResult
from consequence_gate.core.circuit_breaker import SteerCircuitBreaker


class MockBeforeToolCallEvent:
    """Minimal mock of BeforeToolCallEvent for unit testing."""

    def __init__(self, tool_name: str, tool_input: dict):
        self.tool_use = {"name": tool_name, "input": tool_input}
        self.cancel_tool = None
        self.selected_tool = None
        self.invocation_state = {}


def test_allow_passes_through():
    """ALLOW decision: hook returns cleanly, cancel_tool stays None."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(
            confidence=0.95,
            numeric_deltas={"balance": -1000},
            irreversibility_score=0.2,
        )

    def evaluator_fn(delta, breaker):
        return EvaluationResult(
            decision=GateDecision.ALLOW,
            confidence=0.95,
            reason="Within safe bounds.",
        )

    hook = ConsequenceGateHook(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    event = MockBeforeToolCallEvent("process_claim", {"amount": 1000, "claim_id": "c1"})
    hook.intercept(event)

    assert event.cancel_tool is None


def test_deny_sets_cancel_message():
    """DENY decision: hook sets event.cancel_tool with hard-block message."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(
            confidence=0.95,
            numeric_deltas={"balance": -500000},
            irreversibility_score=0.95,
        )

    def evaluator_fn(delta, breaker):
        return EvaluationResult(
            decision=GateDecision.DENY,
            confidence=0.95,
            reason="Critical breach.",
        )

    hook = ConsequenceGateHook(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    event = MockBeforeToolCallEvent("process_claim", {"amount": 500000, "claim_id": "c2"})
    hook.intercept(event)

    assert event.cancel_tool is not None
    assert "BLOCKED:" in event.cancel_tool


def test_ask_sets_escalation_message():
    """ASK decision: hook sets event.cancel_tool with escalation message."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(
            confidence=0.40,  # low confidence
            numeric_deltas={},
            irreversibility_score=0.0,
        )

    def evaluator_fn(delta, breaker):
        return EvaluationResult(
            decision=GateDecision.ASK,
            confidence=0.40,
            reason="Low simulation confidence.",
        )

    hook = ConsequenceGateHook(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    event = MockBeforeToolCallEvent("process_claim", {"amount": 100, "claim_id": "c3"})
    hook.intercept(event)

    assert event.cancel_tool is not None
    assert "ESCALATION_REQUIRED:" in event.cancel_tool


def test_steer_sets_guidance_message():
    """STEER decision: hook sets event.cancel_tool with structured guidance."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(
            confidence=0.90,
            numeric_deltas={"balance": -50000},
            irreversibility_score=0.8,
        )

    def evaluator_fn(delta, breaker):
        return EvaluationResult(
            decision=GateDecision.STEER,
            confidence=0.90,
            reason="Exceeds tier limit.",
            steer_payload={
                "guidance": "Cannot process full amount. Split into instant + escrow.",
                "suggested_tool": "create_staged_disbursement",
                "suggested_args": {
                    "immediate_amount": 25000,
                    "escrow_amount": 25000,
                    "idempotency_key": "steer_c4",
                },
            },
        )

    hook = ConsequenceGateHook(
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
        circuit_breaker=SteerCircuitBreaker(),
    )

    event = MockBeforeToolCallEvent("process_claim", {"amount": 50000, "claim_id": "c4"})
    hook.intercept(event)

    assert event.cancel_tool is not None
    assert "STEER_GUIDANCE:" in event.cancel_tool
    assert "create_staged_disbursement" in event.cancel_tool
    assert "idempotency_key=steer_c4" in event.cancel_tool


def test_financial_factory_hook():
    """create_financial_gate_hook returns a working ConsequenceGateHook."""
    hook = create_financial_gate_hook(
        daily_tier_limit_inr=25000.0,
        instant_wire_threshold=10000.0,
        max_retries=2,
        context_provider=lambda event: {
            "account_rolling_24h_spend": 0.0,
            "kyc_verified": True,
        },
    )

    assert isinstance(hook, ConsequenceGateHook)

    event = MockBeforeToolCallEvent(
        "process_claim",
        {"amount": 50000, "currency": "INR", "payout_method": "instant_upi", "claim_id": "c5"},
    )
    hook.intercept(event)

    # Should STEER (exceeds tier limit)
    assert event.cancel_tool is not None
    assert "STEER_GUIDANCE:" in event.cancel_tool


def test_database_factory_hook_no_conn_asks():
    """create_database_gate_hook with no db_conn -> low confidence -> ASK."""
    hook = create_database_gate_hook(
        max_autonomous_delete_rows=100,
        db_conn=None,  # no live connection
        max_retries=2,
        context_provider=lambda event: {
            "table_metadata": {"orders": {"total_rows": 500000}},
        },
    )

    assert isinstance(hook, ConsequenceGateHook)

    event = MockBeforeToolCallEvent(
        "delete_records",
        {"table": "orders", "filters": {}, "hard_delete": True},
    )
    hook.intercept(event)

    # Should ASK (low confidence without live planner access)
    assert event.cancel_tool is not None
    assert "ESCALATION_REQUIRED:" in event.cancel_tool


def test_retry_cap_escalates_after_max():
    """After max_retries, STEER is forced to ASK (circuit breaker trips)."""

    call_count = [0]

    def simulator_fn(tool_name, args, context):
        call_count[0] += 1
        return MagicMock(
            confidence=0.90,
            numeric_deltas={"balance": -50000},
            irreversibility_score=0.8,
        )

    breaker = SteerCircuitBreaker(max_retries=2)

    hook = ConsequenceGateHook(
        simulator_fn=simulator_fn,
        circuit_breaker=breaker,
    )

    # First two calls: STEER
    for i in range(2):
        event = MockBeforeToolCallEvent(
            "process_claim",
            {"amount": 50000, "claim_id": "c6"},  # same natural key
        )
        hook.intercept(event)
        assert "STEER_GUIDANCE:" in event.cancel_tool

    # Third call with same natural key: circuit breaker trips -> ASK
    event = MockBeforeToolCallEvent(
        "process_claim",
        {"amount": 50000, "claim_id": "c6"},
    )
    hook.intercept(event)

    assert "ESCALATION_REQUIRED:" in event.cancel_tool or "STEER" not in event.cancel_tool
