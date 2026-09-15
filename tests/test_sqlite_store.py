"""Tests for SQLiteStore: durable circuit-breaker state persistence."""

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from consequence_gate.core.models import EvaluationResult, GateDecision
from consequence_gate.core.store import SQLiteStore


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path) # let SQLiteStore create it fresh
    yield path
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Missing / empty keys
# ---------------------------------------------------------------------------

class TestMissingKeys:
    def test_get_attempts_missing_key_returns_zero(self, db_path):
        store = SQLiteStore(db_path)
        assert store.get_attempts("nonexistent") == 0

    def test_get_response_missing_key_returns_none(self, db_path):
        store = SQLiteStore(db_path)
        assert store.get_response("nonexistent") is None

    def test_empty_string_key_attempts(self, db_path):
        store = SQLiteStore(db_path)
        assert store.get_attempts("") == 0
        count = store.increment_attempts("")
        assert count == 1
        assert store.get_attempts("") == 1

    def test_empty_string_key_response(self, db_path):
        store = SQLiteStore(db_path)
        result = EvaluationResult(
            decision=GateDecision.ALLOW, confidence=0.9, reason="ok"
        )
        store.set_response("", result)
        fetched = store.get_response("")
        assert fetched is not None
        assert fetched.decision == GateDecision.ALLOW


# ---------------------------------------------------------------------------
# Attempt round-trip
# ---------------------------------------------------------------------------

class TestAttemptRoundTrip:
    def test_increment_returns_new_count(self, db_path):
        store = SQLiteStore(db_path)
        assert store.increment_attempts("k1") == 1
        assert store.increment_attempts("k1") == 2
        assert store.increment_attempts("k1") == 3

    def test_independent_keys(self, db_path):
        store = SQLiteStore(db_path)
        store.increment_attempts("alpha")
        store.increment_attempts("alpha")
        store.increment_attempts("beta")
        assert store.get_attempts("alpha") == 2
        assert store.get_attempts("beta") == 1


# ---------------------------------------------------------------------------
# Response round-trip (all GateDecision values, steer_payload, evidence)
# ---------------------------------------------------------------------------

class TestResponseRoundTrip:
    def test_set_and_get_allow(self, db_path):
        store = SQLiteStore(db_path)
        result = EvaluationResult(
            decision=GateDecision.ALLOW, confidence=0.95, reason="within bounds"
        )
        store.set_response("key_allow", result)
        fetched = store.get_response("key_allow")
        assert fetched is not None
        assert fetched.decision == GateDecision.ALLOW
        assert fetched.confidence == pytest.approx(0.95)
        assert fetched.reason == "within bounds"
        assert fetched.steer_payload is None
        assert fetched.evidence is None

    def test_set_and_get_steer_with_payload(self, db_path):
        store = SQLiteStore(db_path)
        result = EvaluationResult(
            decision=GateDecision.STEER,
            confidence=0.88,
            reason="velocity breach",
            steer_payload={
                "guidance": "reduce amount",
                "suggested_tool": "staged_payout",
                "suggested_args": {"amount": 5000},
            },
        )
        store.set_response("key_steer", result)
        fetched = store.get_response("key_steer")
        assert fetched is not None
        assert fetched.decision == GateDecision.STEER
        assert fetched.steer_payload == {
            "guidance": "reduce amount",
            "suggested_tool": "staged_payout",
            "suggested_args": {"amount": 5000},
        }

    def test_set_and_get_deny(self, db_path):
        store = SQLiteStore(db_path)
        result = EvaluationResult(
            decision=GateDecision.DENY, confidence=0.97, reason="critical blast"
        )
        store.set_response("key_deny", result)
        fetched = store.get_response("key_deny")
        assert fetched.decision == GateDecision.DENY

    def test_set_and_get_ask(self, db_path):
        store = SQLiteStore(db_path)
        result = EvaluationResult(
            decision=GateDecision.ASK, confidence=0.42, reason="stale context"
        )
        store.set_response("key_ask", result)
        fetched = store.get_response("key_ask")
        assert fetched.decision == GateDecision.ASK
        assert fetched.confidence == pytest.approx(0.42)

    def test_overwrite_response(self, db_path):
        store = SQLiteStore(db_path)
        store.set_response(
            "k", EvaluationResult(decision=GateDecision.ALLOW, confidence=0.9, reason="ok")
        )
        store.set_response(
            "k", EvaluationResult(decision=GateDecision.DENY, confidence=0.99, reason="nope")
        )
        fetched = store.get_response("k")
        assert fetched.decision == GateDecision.DENY
        assert fetched.reason == "nope"


# ---------------------------------------------------------------------------
# Persistence across store instances
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_survives_new_instance(self, db_path):
        s1 = SQLiteStore(db_path)
        s1.increment_attempts("persist_key")
        s1.increment_attempts("persist_key")
        result = EvaluationResult(
            decision=GateDecision.STEER, confidence=0.9, reason="steer"
        )
        s1.set_response("persist_resp", result)

        # Simulate process restart: create a new store pointing at the same file
        s2 = SQLiteStore(db_path)
        assert s2.get_attempts("persist_key") == 2
        fetched = s2.get_response("persist_resp")
        assert fetched is not None
        assert fetched.decision == GateDecision.STEER

    def test_survives_new_circuit_breaker(self, db_path):
        from consequence_gate.core.circuit_breaker import SteerCircuitBreaker

        store = SQLiteStore(db_path)
        b1 = SteerCircuitBreaker(max_retries=2, store=store)
        b1.resolve("shared_txn", {"mod": 1}, 0.9, {"guidance": "a"})
        assert store.get_attempts("steer_shared_txn") == 1

        # New breaker, same store — state carries over
        b2 = SteerCircuitBreaker(max_retries=2, store=store)
        b2.resolve("shared_txn", {"mod": 2}, 0.9, {"guidance": "a"})
        assert store.get_attempts("steer_shared_txn") == 2

        # Third resolve should trip the cap
        b3 = SteerCircuitBreaker(max_retries=2, store=store)
        r3 = b3.resolve("shared_txn", {"mod": 3}, 0.9, {"guidance": "a"})
        from consequence_gate.core.models import GateDecision as GD
        assert r3.decision == GD.ASK


# ---------------------------------------------------------------------------
# Concurrent access
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_increments_are_atomic(self, db_path):
        """SQLite's default busy-timeout serializes writes; every increment
        must be accounted for with no lost updates."""
        store = SQLiteStore(db_path)
        n_threads = 10
        n_per_thread = 50

        def worker(_):
            for _ in range(n_per_thread):
                store.increment_attempts("concurrent_key")

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            list(pool.map(worker, range(n_threads)))

        assert store.get_attempts("concurrent_key") == n_threads * n_per_thread
