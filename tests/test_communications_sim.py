"""
Unit tests for OutboundCommunicationSimulator.

Tests cover:
- ALLOW: within safe recipient bounds, compliance met
- DENY: compliance violation (no unsubscribe suppression)
- DENY: severe reputation risk
- ASK: low confidence (missing segment/historical data)
- STEER: over autonomous threshold -> canary recommendation
- STEER: canary predicts high bounce/complaint -> list hygiene recommendation
- Idempotency token stability across retries
"""

from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.core.models import GateDecision
from consequence_gate.simulators.communications import OutboundCommunicationSimulator


def test_allow_within_safe_bounds():
    """ALLOW: recipient count within threshold, compliance met."""
    sim = OutboundCommunicationSimulator(max_autonomous_recipients=10000)
    breaker = SteerCircuitBreaker()

    args = {
        "channel": "email",
        "recipients": ["user1@example.com"] * 5000,
        "suppress_unsubscribes": True,
        "canary_enabled": False,
    }
    context = {
        "segment_counts": {"active_users": 5000},
        "historical_bounce_rate": 0.02,
        "historical_complaint_rate": 0.005,
        "recent_unsubscribes": set(),
    }

    delta = sim.simulate("send_campaign", args, context)
    result = sim.evaluate(delta, breaker)

    assert result.decision == GateDecision.ALLOW
    assert delta.confidence == 0.90


def test_deny_compliance_violation():
    """DENY: no unsubscribe suppression -> hard compliance violation."""
    sim = OutboundCommunicationSimulator(max_autonomous_recipients=10000)
    breaker = SteerCircuitBreaker()

    args = {
        "channel": "email",
        "recipients": ["user1@example.com"] * 1000,
        "suppress_unsubscribes": False,
        "canary_enabled": False,
    }
    context = {
        "segment_counts": {"active_users": 1000},
        "historical_bounce_rate": 0.02,
        "historical_complaint_rate": 0.005,
    }

    delta = sim.simulate("send_campaign", args, context)
    result = sim.evaluate(delta, breaker)

    assert result.decision == GateDecision.DENY
    assert "COMPLIANCE VIOLATION" in result.reason


def test_deny_severe_reputation_risk():
    """DENY: severe sender reputation impact."""
    sim = OutboundCommunicationSimulator(max_autonomous_recipients=100000)
    breaker = SteerCircuitBreaker()

    args = {
        "channel": "sms",
        "recipients": ["+1234567890"] * 50000,
        "suppress_unsubscribes": True,
        "canary_enabled": True,
    }
    context = {
        "segment_counts": {"all_users": 50000},
        "historical_bounce_rate": 0.02,
        "historical_complaint_rate": 0.05,
        "recent_unsubscribes": set(),
    }

    delta = sim.simulate("send_sms_blast", args, context)
    result = sim.evaluate(delta, breaker)

    assert result.decision in (GateDecision.DENY, GateDecision.STEER)
    assert delta.sender_reputation_impact < -0.5


def test_ask_low_confidence():
    """ASK: missing segment/historical data -> low confidence."""
    sim = OutboundCommunicationSimulator(max_autonomous_recipients=10000)
    breaker = SteerCircuitBreaker()

    args = {
        "channel": "email",
        "recipients": ["user1@example.com"] * 5000,
        "suppress_unsubscribes": True,
    }
    context = {}

    delta = sim.simulate("send_campaign", args, context)
    result = sim.evaluate(delta, breaker)

    assert result.decision == GateDecision.ASK
    assert delta.confidence < 0.60


def test_steer_over_threshold():
    """STEER: over autonomous threshold -> canary recommendation."""
    sim = OutboundCommunicationSimulator(max_autonomous_recipients=10000)
    breaker = SteerCircuitBreaker()

    args = {
        "channel": "email",
        "recipients": ["user1@example.com"] * 50000,
        "suppress_unsubscribes": True,
        "canary_enabled": False,
    }
    context = {
        "segment_counts": {"active_users": 50000},
        "historical_bounce_rate": 0.02,
        "historical_complaint_rate": 0.005,
    }

    delta = sim.simulate("send_campaign", args, context)
    result = sim.evaluate(delta, breaker)

    assert result.decision == GateDecision.STEER
    assert "canary" in result.steer_payload["guidance"].lower()
    assert result.steer_payload["suggested_tool"] == "send_canary_cohort"


def test_steer_high_bounce_canary():
    """STEER: canary predicts high bounce -> list hygiene recommendation."""
    sim = OutboundCommunicationSimulator(
        max_autonomous_recipients=100000,
        canary_max_bounce_rate=0.05,
    )
    breaker = SteerCircuitBreaker()

    args = {
        "channel": "email",
        "recipients": ["user1@example.com"] * 50000,
        "suppress_unsubscribes": True,
        "canary_enabled": True,
    }
    context = {
        "segment_counts": {"active_users": 50000},
        "historical_bounce_rate": 0.15,
        "historical_complaint_rate": 0.005,
    }

    delta = sim.simulate("send_campaign", args, context)
    result = sim.evaluate(delta, breaker)

    assert result.decision == GateDecision.STEER
    assert "list hygiene" in result.steer_payload["guidance"].lower()
    assert result.steer_payload["suggested_tool"] == "run_list_hygiene"


def test_irreversibility_scoring():
    """Test irreversibility scoring by channel."""
    sim = OutboundCommunicationSimulator()

    email_args = {"channel": "email", "recipients": ["a@b.com"]}
    email_delta = sim.simulate("send", email_args, {})
    assert email_delta.irreversibility_score == 1.0

    sms_args = {"channel": "sms", "recipients": ["+123"]}
    sms_delta = sim.simulate("send", sms_args, {})
    assert sms_delta.irreversibility_score == 1.0

    push_args = {"channel": "push_notification", "recipients": ["device_123"]}
    push_delta = sim.simulate("send", push_args, {})
    assert push_delta.irreversibility_score == 0.8

    inapp_args = {"channel": "in_app_notification", "recipients": ["user_123"]}
    inapp_delta = sim.simulate("send", inapp_args, {})
    assert inapp_delta.irreversibility_score == 0.3


def test_idempotency_token_stable():
    """Same natural key -> same idempotency token across retries."""
    sim = OutboundCommunicationSimulator(max_autonomous_recipients=10000)
    breaker = SteerCircuitBreaker(max_retries=2)

    args = {
        "channel": "email",
        "recipients": ["user1@example.com"] * 50000,
        "suppress_unsubscribes": True,
        "campaign_id": "camp_123",
    }
    context = {
        "segment_counts": {"active_users": 50000},
        "historical_bounce_rate": 0.02,
        "historical_complaint_rate": 0.005,
    }

    delta = sim.simulate("send_campaign", args, context)
    r1 = sim.evaluate(delta, breaker)
    r2 = sim.evaluate(delta, breaker)

    assert r1.decision == GateDecision.STEER
    assert (
        r1.steer_payload["suggested_args"]["idempotency_key"]
        == r2.steer_payload["suggested_args"]["idempotency_key"]
    )
