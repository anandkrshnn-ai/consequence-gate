import json
import sqlite3
from typing import Protocol, Any
from consequence_gate.core.models import EvaluationResult, GateDecision

class Store(Protocol):
    """Protocol for durable circuit breaker state."""
    
    def get_attempts(self, key: str) -> int:
        """Get the current attempt count for a natural key."""
        ...

    def increment_attempts(self, key: str) -> int:
        """Increment and return the new attempt count for a natural key."""
        ...

    def get_response(self, key: str) -> EvaluationResult | None:
        """Get a cached EvaluationResult for a specific payload hash."""
        ...

    def set_response(self, key: str, result: EvaluationResult) -> None:
        """Cache an EvaluationResult for a specific payload hash."""
        ...


class InMemoryStore:
    """Default in-memory state store. Does not survive process restarts."""
    
    def __init__(self):
        self._attempts: dict[str, int] = {}
        self._responses: dict[str, EvaluationResult] = {}

    def get_attempts(self, key: str) -> int:
        return self._attempts.get(key, 0)

    def increment_attempts(self, key: str) -> int:
        self._attempts[key] = self._attempts.get(key, 0) + 1
        return self._attempts[key]

    def get_response(self, key: str) -> EvaluationResult | None:
        return self._responses.get(key)

    def set_response(self, key: str, result: EvaluationResult) -> None:
        self._responses[key] = result


class SQLiteStore:
    """Durable state store using SQLite."""
    
    def __init__(self, db_path: str = "consequence_gate_state.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS attempts (
                    key TEXT PRIMARY KEY,
                    count INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS responses (
                    key TEXT PRIMARY KEY,
                    decision TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reason TEXT NOT NULL,
                    steer_payload TEXT,
                    evidence TEXT
                )
            """)

    def get_attempts(self, key: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT count FROM attempts WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else 0

    def increment_attempts(self, key: str) -> int:
        """Atomic increment via a single UPSERT statement.

        The previous read-then-write pattern (SELECT, then INSERT or UPDATE)
        was not atomic: two concurrent callers could both see the key absent
        and both attempt INSERT, causing a UNIQUE constraint failure. The
        ON CONFLICT DO UPDATE form is a single SQLite statement and is
        serialized by SQLite's write lock, so no lost updates or races.
        """
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                "INSERT INTO attempts (key, count) VALUES (?, 1) "
                "ON CONFLICT(key) DO UPDATE SET count = count + 1",
                (key,),
            )
            cursor = conn.execute("SELECT count FROM attempts WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else 0

    def get_response(self, key: str) -> EvaluationResult | None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT decision, confidence, reason, steer_payload, evidence FROM responses WHERE key = ?", 
                (key,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            
            decision, confidence, reason, steer_payload_str, evidence_str = row
            steer_payload = json.loads(steer_payload_str) if steer_payload_str else None
            evidence = json.loads(evidence_str) if evidence_str else None
            
            return EvaluationResult(
                decision=GateDecision(decision),
                confidence=float(confidence),
                reason=reason,
                steer_payload=steer_payload,
                evidence=evidence
            )

    def set_response(self, key: str, result: EvaluationResult) -> None:
        with sqlite3.connect(self.db_path) as conn:
            steer_payload_str = json.dumps(result.steer_payload) if result.steer_payload else None
            evidence_str = json.dumps(result.evidence) if result.evidence else None
            
            conn.execute("""
                INSERT OR REPLACE INTO responses (key, decision, confidence, reason, steer_payload, evidence)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (key, result.decision.value, result.confidence, result.reason, steer_payload_str, evidence_str))
