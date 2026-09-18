"""Callback interfaces for human-in-the-loop approval."""

from enum import Enum
from typing import Any, Protocol

from consequence_gate.core.models import EvaluationResult


class ApprovalDecision(str, Enum):
    APPROVED = "APPROVED"  # human approved -> treat as ALLOW
    REJECTED = "REJECTED"  # human rejected -> treat as DENY
    TIMEOUT = "TIMEOUT"  # no response within deadline -> default DENY


class AskCallback(Protocol):
    def __call__(self, result: EvaluationResult, evidence: Any) -> ApprovalDecision: ...
