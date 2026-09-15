"""Ed25519 notary for gate decisions.

Produces a self-contained envelope: payload hash + public key + signature.
Offline verification needs only the envelope — not the simulators.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, Field

from .models import EvaluationResult, GateDecision


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def canonical_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class ConsequenceEvidenceEnvelope(BaseModel):
    tool_name: str
    proposed_args: dict[str, Any]
    numeric_deltas: dict[str, float] = Field(default_factory=dict)
    irreversibility_score: float
    confidence: float
    decision: str
    reason: str
    natural_key: str | None = None
    simulated_side_effects: list[str] = Field(default_factory=list)
    issued_at: str
    payload_hash: str
    public_key: str
    signature: str

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "proposed_args": self.proposed_args,
            "numeric_deltas": self.numeric_deltas,
            "irreversibility_score": self.irreversibility_score,
            "confidence": self.confidence,
            "decision": self.decision,
            "reason": self.reason,
            "natural_key": self.natural_key,
            "simulated_side_effects": self.simulated_side_effects,
            "issued_at": self.issued_at,
        }

    def to_json(self) -> str:
        return self.model_dump_json()


class ConsequenceNotary:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._private = private_key
        self._public = private_key.public_key()

    @classmethod
    def ephemeral(cls) -> ConsequenceNotary:
        return cls(Ed25519PrivateKey.generate())

    @property
    def public_key_b64(self) -> str:
        return _b64(self._public.public_bytes_raw())

    def notarize(
        self,
        *,
        tool_name: str,
        proposed_args: dict[str, Any],
        numeric_deltas: dict[str, float],
        irreversibility_score: float,
        confidence: float,
        decision: GateDecision | str,
        reason: str,
        natural_key: str | None = None,
        simulated_side_effects: list[str] | None = None,
        issued_at: str | None = None,
    ) -> ConsequenceEvidenceEnvelope:
        issued = issued_at or datetime.now(timezone.utc).isoformat()
        decision_s = decision.value if isinstance(decision, GateDecision) else str(decision)
        unsigned = {
            "tool_name": tool_name,
            "proposed_args": proposed_args,
            "numeric_deltas": numeric_deltas,
            "irreversibility_score": irreversibility_score,
            "confidence": confidence,
            "decision": decision_s,
            "reason": reason,
            "natural_key": natural_key,
            "simulated_side_effects": list(simulated_side_effects or []),
            "issued_at": issued,
        }
        payload = canonical_dumps(unsigned).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        signature = _b64(self._private.sign(payload))
        return ConsequenceEvidenceEnvelope(
            **unsigned,
            payload_hash=digest,
            public_key=self.public_key_b64,
            signature=signature,
        )

    def sign_evaluation(self, delta: Any, result: EvaluationResult) -> ConsequenceEvidenceEnvelope:
        numeric = getattr(delta, "numeric_deltas", None)
        if numeric is None:
            numeric = {}
            if hasattr(delta, "projected_net_delta_inr"):
                numeric["projected_net_delta_inr"] = float(delta.projected_net_delta_inr)
            if hasattr(delta, "estimated_affected_rows"):
                numeric["estimated_affected_rows"] = float(delta.estimated_affected_rows)
            if hasattr(delta, "rolling_24h_exposure_inr"):
                numeric["rolling_24h_exposure_inr"] = float(delta.rolling_24h_exposure_inr)

        proposed = getattr(delta, "proposed_args", None)
        if proposed is None:
            proposed = {}
            if hasattr(delta, "target_table"):
                proposed = {
                    "table": delta.target_table,
                    "estimated_affected_rows": getattr(delta, "estimated_affected_rows", 0),
                }

        return self.notarize(
            tool_name=getattr(delta, "tool_name", "unknown"),
            proposed_args=dict(proposed),
            numeric_deltas={k: float(v) for k, v in numeric.items()},
            irreversibility_score=float(getattr(delta, "irreversibility_score", 0.0)),
            confidence=float(result.confidence),
            decision=result.decision,
            reason=result.reason,
            natural_key=getattr(delta, "natural_key", None),
            simulated_side_effects=list(getattr(delta, "simulated_side_effects", []) or []),
        )


def verify_envelope(envelope: ConsequenceEvidenceEnvelope) -> bool:
    payload = canonical_dumps(envelope.unsigned_payload()).encode("utf-8")
    expected = hashlib.sha256(payload).hexdigest()
    if expected != envelope.payload_hash:
        return False
    try:
        public = Ed25519PublicKey.from_public_bytes(_b64d(envelope.public_key))
        public.verify(_b64d(envelope.signature), payload)
    except (InvalidSignature, ValueError, Exception):
        return False
    return True


def attach_evidence(
    result: EvaluationResult, delta: Any, notary: ConsequenceNotary | None
) -> EvaluationResult:
    signer = notary or ConsequenceNotary.ephemeral()
    result.evidence = signer.sign_evaluation(delta, result)
    return result
