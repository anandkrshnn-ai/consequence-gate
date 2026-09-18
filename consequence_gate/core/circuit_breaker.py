"""
SteerCircuitBreaker: idempotency-locked retry cap for STEER decisions.

Design contract (see project history / design notes):
- The idempotency token is derived ONCE from the transaction's own natural
  key (e.g. claim_id, table+filter hash) -- never regenerated per retry.
  A fresh UUID per attempt defeats duplicate-execution protection.
- Responses are cached per token, so a retry with the same natural key
  returns the cached result instead of re-executing (Stripe-style contract).
- Retry count is tracked server-side per token, with a hard cap. Once
  exceeded, the breaker forces ASK (human escalation) regardless of how
  good the steering guidance is -- this is a backstop against
  loop-thrashing, independent of guidance quality.
"""

import hashlib
import json
from typing import Any

from .models import EvaluationResult, GateDecision
from .store import InMemoryStore, Store


def _normalize_payload(val: Any) -> Any:
    """Normalizes payload structures to ensure consistent hashing."""
    if isinstance(val, dict):
        return {str(k): _normalize_payload(v) for k, v in val.items()}
    elif isinstance(val, (list, tuple, set)):
        return [_normalize_payload(v) for v in val]
    elif isinstance(val, float):
        # Round floats to prevent precision mismatch from causing distinct hashes
        return round(val, 4)
    elif val is None or isinstance(val, (int, str, bool)):
        return val
    else:
        # Fallback to string representation for non-native types (e.g. datetime)
        return str(val)


def _hash_payload(payload: dict[str, Any]) -> str:
    normalized = _normalize_payload(payload)
    serialized = json.dumps(normalized, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class SteerCircuitBreaker:
    def __init__(self, max_retries: int = 2, store: Store | None = None):
        self.max_retries = max_retries
        self.store = store or InMemoryStore()

    def token_for(self, natural_key: str) -> str:
        return f"steer_{natural_key}"

    def resolve(
        self,
        natural_key: str,
        payload: dict[str, Any],
        confidence: float,
        base_steer: dict[str, Any],
    ) -> EvaluationResult:
        attempt_key = self.token_for(natural_key)
        payload_hash = _hash_payload(payload)
        response_key = f"{attempt_key}_{payload_hash}"

        cached = self.store.get_response(response_key)
        if cached is not None:
            return cached

        attempt = self.store.get_attempts(attempt_key)

        if attempt >= self.max_retries:
            result = EvaluationResult(
                decision=GateDecision.ASK,
                confidence=confidence,
                reason=f"Steer circuit breaker tripped ({attempt}/{self.max_retries}). Escalating to human.",
            )
            self.store.increment_attempts(attempt_key)
            self.store.set_response(response_key, result)
            return result

        self.store.increment_attempts(attempt_key)
        base_steer.setdefault("suggested_args", {})["idempotency_key"] = attempt_key

        result = EvaluationResult(
            decision=GateDecision.STEER,
            confidence=confidence,
            reason=f"Steered to safer path (attempt {attempt + 1}/{self.max_retries}).",
            steer_payload=base_steer,
        )
        self.store.set_response(response_key, result)
        return result
