from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.core.models import GateDecision
from consequence_gate.simulators.financial import FinancialDeltaPredictor


def test_steer_on_tier_breach():
    predictor = FinancialDeltaPredictor(daily_tier_limit_inr=25000.0)
    breaker = SteerCircuitBreaker(max_retries=2)
    args = {"amount": 50000, "currency": "INR", "payout_method": "instant_upi", "claim_id": "c1"}
    context = {"account_rolling_24h_spend": 0.0, "kyc_verified": True}
    delta = predictor.simulate("process_claim", args, context)
    result = predictor.evaluate(delta, breaker)
    assert result.decision == GateDecision.STEER


def test_idempotency_token_stable_across_retries():
    predictor = FinancialDeltaPredictor(daily_tier_limit_inr=25000.0)
    breaker = SteerCircuitBreaker(max_retries=2)
    args = {"amount": 50000, "currency": "INR", "payout_method": "instant_upi", "claim_id": "c1"}
    context = {"account_rolling_24h_spend": 0.0, "kyc_verified": True}
    delta = predictor.simulate("process_claim", args, context)
    r1 = predictor.evaluate(delta, breaker)
    r2 = predictor.evaluate(delta, breaker)
    assert (
        r1.steer_payload["suggested_args"]["idempotency_key"]
        == r2.steer_payload["suggested_args"]["idempotency_key"]
        or r2.decision != GateDecision.STEER
    )


def test_low_confidence_always_asks():
    predictor = FinancialDeltaPredictor()
    breaker = SteerCircuitBreaker()
    args = {"amount": 100, "currency": "INR", "claim_id": "c2"}
    context = {}
    delta = predictor.simulate("process_claim", args, context)
    result = predictor.evaluate(delta, breaker)
    assert result.decision == GateDecision.ASK
