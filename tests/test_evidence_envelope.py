"""Consequence Evidence Notary — signed envelope for every gate decision."""

from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.core.evidence import (
    ConsequenceEvidenceEnvelope,
    ConsequenceNotary,
    verify_envelope,
)
from consequence_gate.core.models import GateDecision
from consequence_gate.simulators.database import DataDeletionSimulator
from consequence_gate.simulators.financial import FinancialDeltaPredictor


def _notary() -> ConsequenceNotary:
    return ConsequenceNotary(Ed25519PrivateKey.generate())


def test_financial_allow_emits_signed_envelope():
    predictor = FinancialDeltaPredictor(daily_tier_limit_inr=25000.0)
    predictor.notary = _notary()
    breaker = SteerCircuitBreaker()
    args = {
        "amount": 1000,
        "currency": "INR",
        "payout_method": "standard_ach",
        "claim_id": "c-allow",
    }
    context = {"account_rolling_24h_spend": 0.0, "kyc_verified": True}
    delta = predictor.simulate("process_claim", args, context)
    result = predictor.evaluate(delta, breaker)

    assert result.decision == GateDecision.ALLOW
    assert isinstance(result.evidence, ConsequenceEvidenceEnvelope)
    assert result.evidence.tool_name == "process_claim"
    assert result.evidence.proposed_args["claim_id"] == "c-allow"
    assert result.evidence.decision == "ALLOW"
    assert result.evidence.irreversibility_score == delta.irreversibility_score
    assert result.evidence.signature
    assert verify_envelope(result.evidence) is True


def test_financial_deny_and_steer_are_signed():
    predictor = FinancialDeltaPredictor(daily_tier_limit_inr=25000.0)
    predictor.notary = _notary()
    breaker = SteerCircuitBreaker()

    deny_args = {
        "amount": 80000,
        "currency": "INR",
        "payout_method": "instant_upi",
        "claim_id": "c-deny",
    }
    context = {"account_rolling_24h_spend": 0.0, "kyc_verified": True}
    deny = predictor.evaluate(predictor.simulate("process_claim", deny_args, context), breaker)
    assert deny.decision == GateDecision.DENY
    assert deny.evidence is not None
    assert deny.evidence.decision == "DENY"
    assert verify_envelope(deny.evidence) is True

    steer_args = {
        "amount": 30000,
        "currency": "INR",
        "payout_method": "instant_upi",
        "claim_id": "c-steer",
    }
    steer = predictor.evaluate(predictor.simulate("process_claim", steer_args, context), breaker)
    assert steer.decision == GateDecision.STEER
    assert steer.evidence is not None
    assert steer.evidence.decision == "STEER"
    assert verify_envelope(steer.evidence) is True


def test_database_evaluate_emits_signed_envelope():
    sim = DataDeletionSimulator(max_autonomous_delete_rows=100, db_conn=None)
    sim.notary = _notary()
    delta = sim.simulate(
        "delete_rows",
        {"table": "claims", "filters": {"id": "1"}, "hard_delete": False},
        {"table_metadata": {"claims": {"total_rows": 3, "cascade_children": []}}},
    )
    result = sim.evaluate(delta, SteerCircuitBreaker())
    assert result.decision == GateDecision.ASK
    assert result.evidence is not None
    assert result.evidence.tool_name == "delete_rows"
    assert verify_envelope(result.evidence) is True


def test_tampered_envelope_fails_verification():
    predictor = FinancialDeltaPredictor()
    predictor.notary = _notary()
    args = {"amount": 100, "currency": "INR", "claim_id": "c-tamper"}
    context = {"account_rolling_24h_spend": 0.0, "kyc_verified": True}
    result = predictor.evaluate(
        predictor.simulate("process_claim", args, context), SteerCircuitBreaker()
    )
    envelope = result.evidence
    assert envelope is not None
    envelope.decision = "DENY" if envelope.decision != "DENY" else "ALLOW"
    assert verify_envelope(envelope) is False


def test_root_verifier_does_not_import_simulators(tmp_path: Path):
    predictor = FinancialDeltaPredictor()
    predictor.notary = _notary()
    args = {"amount": 100, "currency": "INR", "claim_id": "c-cli"}
    context = {"account_rolling_24h_spend": 0.0, "kyc_verified": True}
    result = predictor.evaluate(
        predictor.simulate("process_claim", args, context), SteerCircuitBreaker()
    )
    out = tmp_path / "envelope.json"
    out.write_text(result.evidence.to_json())

    import verify as root_verify

    loaded = json.loads(out.read_text())
    assert root_verify.verify_dict(loaded) is True
