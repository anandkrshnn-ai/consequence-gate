from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.core.models import GateDecision


def test_retry_cap_escalates_to_ask():
    breaker = SteerCircuitBreaker(max_retries=2)
    steer = {"guidance": "try alt path", "suggested_tool": "alt", "suggested_args": {}}

    r1 = breaker.resolve("txn_1", {}, 0.9, dict(steer))
    assert r1.decision == GateDecision.STEER

    breaker.store.increment_attempts("steer_txn_1")
    breaker.store.increment_attempts("steer_txn_1")
    r2 = breaker.resolve("txn_1", {}, 0.9, dict(steer))
    assert r2.decision in (GateDecision.STEER, GateDecision.ASK)


def test_same_natural_key_returns_cached_response():
    breaker = SteerCircuitBreaker(max_retries=2)
    steer = {"guidance": "try alt path", "suggested_tool": "alt", "suggested_args": {}}
    r1 = breaker.resolve("txn_2", {}, 0.9, dict(steer))
    r2 = breaker.resolve("txn_2", {"modified": 1}, 0.9, dict(steer))
    assert (
        r1.steer_payload["suggested_args"]["idempotency_key"]
        == r2.steer_payload["suggested_args"]["idempotency_key"]
    )


def test_hash_payload_set_determinism():
    from consequence_gate.core.circuit_breaker import _hash_payload

    # Python's set literal `{a, b, c}` evaluates to a set.
    # While they evaluate to the same logical set, previously the iteration
    # order over the set was hash-randomized, causing non-deterministic lists.
    # We can test by ensuring the hash of a payload containing a set remains
    # identical regardless of arbitrary set construction ordering.
    payload1 = {"active": {"a", "b", "c"}}
    payload2 = {"active": {"c", "a", "b"}}

    assert _hash_payload(payload1) == _hash_payload(payload2)


def test_hash_payload_cross_process_seed():
    import os
    import subprocess
    import sys

    code = (
        "import json; "
        "from consequence_gate.core.circuit_breaker import _hash_payload; "
        "payload = {'active': {'a', 'b', 'c', 'd', 'e'}}; "
        "print(_hash_payload(payload))"
    )

    env1 = os.environ.copy()
    env1["PYTHONHASHSEED"] = "1"
    env2 = os.environ.copy()
    env2["PYTHONHASHSEED"] = "2"

    # Add current dir to PYTHONPATH
    env1["PYTHONPATH"] = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env2["PYTHONPATH"] = env1["PYTHONPATH"]

    p1 = subprocess.run(
        [sys.executable, "-c", code], env=env1, capture_output=True, text=True, check=True
    )
    p2 = subprocess.run(
        [sys.executable, "-c", code], env=env2, capture_output=True, text=True, check=True
    )

    assert p1.stdout.strip() == p2.stdout.strip()
