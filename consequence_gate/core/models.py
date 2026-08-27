"""
Core data models shared across all domain simulators.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GateDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ASK = "ASK"
    STEER = "STEER"


@dataclass
class SimulatedStateDelta:
    """Generic projected-outcome envelope. Domain simulators subclass or
    populate this with their own numeric_deltas / side-effect semantics."""

    tool_name: str
    proposed_args: dict[str, Any]
    numeric_deltas: dict[str, float] = field(default_factory=dict)
    irreversibility_score: float = 0.0  # 0.0 fully reversible -> 1.0 irreversible
    confidence: float = 0.0  # 0.0 -> 1.0, simulator's own confidence in this projection
    simulated_side_effects: list[str] = field(default_factory=list)
    natural_key: str | None = None  # stable identity for idempotency (NOT a random token)


@dataclass
class EvaluationResult:
    decision: GateDecision
    confidence: float
    reason: str
    steer_payload: dict[str, Any] | None = None
