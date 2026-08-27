"""
BlastRadiusEvaluator: generic threshold evaluation for simulated deltas.
Domain simulators typically implement their own evaluate() with
domain-specific rules, but this provides a reusable default.
"""

from .models import GateDecision, SimulatedStateDelta


class BlastRadiusEvaluator:
    def __init__(
        self, max_irreversible_value: float = 1000.0, min_confidence_for_autopass: float = 0.85
    ):
        self.max_irreversible_value = max_irreversible_value
        self.min_confidence_for_autopass = min_confidence_for_autopass

    def evaluate(self, delta: SimulatedStateDelta) -> GateDecision:
        if delta.confidence < self.min_confidence_for_autopass:
            return GateDecision.ASK

        breach = any(abs(v) > self.max_irreversible_value for v in delta.numeric_deltas.values())

        if delta.irreversibility_score >= 0.9 and breach:
            return GateDecision.STEER
        if breach:
            return GateDecision.ASK
        return GateDecision.ALLOW
