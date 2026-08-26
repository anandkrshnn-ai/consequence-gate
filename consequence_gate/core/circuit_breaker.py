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

from typing import Any, Dict
from .models import GateDecision, EvaluationResult


class SteerCircuitBreaker:
    def __init__(self, max_retries: int = 2):
        self.max_retries = max_retries
        self._attempts: Dict[str, int] = {}
        self._responses: Dict[str, EvaluationResult] = {}

    def token_for(self, natural_key: str) -> str:
        return f"steer_{natural_key}"

    def resolve(self, natural_key: str, confidence: float,
                base_steer: Dict[str, Any]) -> EvaluationResult:
        token = self.token_for(natural_key)

        if token in self._responses:
            return self._responses[token]

        attempt = self._attempts.get(token, 0)

        if attempt >= self.max_retries:
            result = EvaluationResult(
                decision=GateDecision.ASK,
                confidence=confidence,
                reason=f"Steer circuit breaker tripped ({attempt}/{self.max_retries}). Escalating to human.",
            )
            self._responses[token] = result
            return result

        self._attempts[token] = attempt + 1
        base_steer.setdefault("suggested_args", {})["idempotency_key"] = token

        result = EvaluationResult(
            decision=GateDecision.STEER,
            confidence=confidence,
            reason=f"Steered to safer path (attempt {attempt + 1}/{self.max_retries}).",
            steer_payload=base_steer,
        )
        return result
