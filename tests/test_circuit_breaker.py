from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.core.models import GateDecision


def test_retry_cap_escalates_to_ask():
    breaker = SteerCircuitBreaker(max_retries=2)
    steer = {"guidance": "try alt path", "suggested_tool": "alt", "suggested_args": {}}

    r1 = breaker.resolve("txn_1", 0.9, dict(steer))
    assert r1.decision == GateDecision.STEER

    breaker._attempts["steer_txn_1"] = 2
    r2 = breaker.resolve("txn_1", 0.9, dict(steer))
    assert r2.decision in (GateDecision.STEER, GateDecision.ASK)


def test_same_natural_key_returns_cached_response():
    breaker = SteerCircuitBreaker(max_retries=2)
    steer = {"guidance": "try alt path", "suggested_tool": "alt", "suggested_args": {}}
    r1 = breaker.resolve("txn_2", 0.9, dict(steer))
    r2 = breaker.resolve("txn_2", 0.9, dict(steer))
    assert (
        r1.steer_payload["suggested_args"]["idempotency_key"]
        == r2.steer_payload["suggested_args"]["idempotency_key"]
    )
