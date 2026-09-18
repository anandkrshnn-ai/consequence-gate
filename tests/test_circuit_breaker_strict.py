from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.core.models import GateDecision


def test_strict_idempotency_caching():
    breaker = SteerCircuitBreaker(max_retries=2)
    steer = {"guidance": "try alt path"}
    payload = {"foo": "bar"}

    r1 = breaker.resolve("txn_strict", payload, 0.9, steer.copy())
    assert r1.decision == GateDecision.STEER
    assert r1.reason == "Steered to safer path (attempt 1/2)."

    # Replay identical payload
    r2 = breaker.resolve("txn_strict", payload, 0.9, steer.copy())
    assert r2.decision == GateDecision.STEER
    assert r2.reason == "Steered to safer path (attempt 1/2)."

    # Counter shouldn't have incremented
    assert breaker.store.get_attempts("steer_txn_strict") == 1


def test_strict_retry_cap_enforcement():
    breaker = SteerCircuitBreaker(max_retries=2)
    steer = {"guidance": "try alt path"}

    # Attempt 1
    r1 = breaker.resolve("txn_loop", {"mod": 1}, 0.9, steer.copy())
    assert r1.decision == GateDecision.STEER

    # Attempt 2
    r2 = breaker.resolve("txn_loop", {"mod": 2}, 0.9, steer.copy())
    assert r2.decision == GateDecision.STEER

    # Attempt 3 - hits cap
    r3 = breaker.resolve("txn_loop", {"mod": 3}, 0.9, steer.copy())
    assert r3.decision == GateDecision.ASK
    assert r3.reason == "Steer circuit breaker tripped (2/2). Escalating to human."

    assert breaker.store.get_attempts("steer_txn_loop") == 3

    # Attempt 4 - should stay blocked permanently
    r4 = breaker.resolve("txn_loop", {"mod": 4}, 0.9, steer.copy())
    assert r4.decision == GateDecision.ASK

    # Replay of attempt 3 (identical payload to cap trip)
    r3_replay = breaker.resolve("txn_loop", {"mod": 3}, 0.9, steer.copy())
    assert r3_replay.decision == GateDecision.ASK


def test_store_survives_across_breakers():
    from consequence_gate.core.store import InMemoryStore

    store = InMemoryStore()

    b1 = SteerCircuitBreaker(max_retries=2, store=store)
    b1.resolve("shared_txn", {"mod": 1}, 0.9, {"guidance": "a"})
    assert store.get_attempts("steer_shared_txn") == 1

    b2 = SteerCircuitBreaker(max_retries=2, store=store)
    b2.resolve("shared_txn", {"mod": 2}, 0.9, {"guidance": "a"})
    assert store.get_attempts("steer_shared_txn") == 2

    # Attempt 3 on new breaker instance should hit cap
    b3 = SteerCircuitBreaker(max_retries=2, store=store)
    r3 = b3.resolve("shared_txn", {"mod": 3}, 0.9, {"guidance": "a"})
    assert r3.decision == GateDecision.ASK
    assert store.get_attempts("steer_shared_txn") == 3
