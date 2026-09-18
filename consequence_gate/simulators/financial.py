"""
FinancialDeltaPredictor: outcome simulator for disbursements, claims, refunds.
Sub-5ms evaluation budget (well within published MCP-proxy overhead norms).
"""

from dataclasses import dataclass, field
from typing import Any

from ..core.circuit_breaker import SteerCircuitBreaker
from ..core.evidence import ConsequenceNotary, attach_evidence
from ..core.models import EvaluationResult, GateDecision
from ..core.thresholds import MIN_CONFIDENCE_AUTOPASS


@dataclass
class FinancialStateDelta:
    tool_name: str
    proposed_args: dict[str, Any]
    projected_net_delta_inr: float
    rolling_24h_exposure_inr: float
    policy_tier_limit_inr: float
    irreversibility_score: float
    confidence: float
    natural_key: str
    simulated_side_effects: list[str] = field(default_factory=list)


class FinancialDeltaPredictor:
    def __init__(
        self, daily_tier_limit_inr: float = 25000.0, instant_wire_threshold: float = 10000.0
    ):
        self.daily_tier_limit_inr = daily_tier_limit_inr
        self.instant_wire_threshold = instant_wire_threshold
        self.notary: ConsequenceNotary | None = None

    def simulate(
        self, tool_name: str, args: dict[str, Any], context: dict[str, Any]
    ) -> FinancialStateDelta:
        amount = float(args.get("amount", 0.0))
        currency = args.get("currency", "INR").upper()
        payout_method = args.get("payout_method", "standard_ach")
        natural_key = (
            args.get("claim_id") or args.get("transaction_ref") or args.get("idempotency_key") or ""
        )

        conversion_rate = (
            1.0 if currency == "INR" else context.get("exchange_rates", {}).get(currency, 0.0)
        )
        net_amount_inr = amount * conversion_rate

        current_24h_spend = context.get("account_rolling_24h_spend", 0.0)
        projected_24h_spend = current_24h_spend + net_amount_inr

        irreversibility = 1.0 if payout_method in ("instant_upi", "rtgs", "wire") else 0.4

        has_verified_kyc = context.get("kyc_verified", False)
        has_fresh_balance = "account_rolling_24h_spend" in context
        if has_verified_kyc and has_fresh_balance:
            confidence = 0.95
        elif has_verified_kyc or has_fresh_balance:
            confidence = 0.80
        else:
            confidence = 0.45

        if not natural_key:
            confidence = 0.0

        side_effects = []
        if projected_24h_spend > self.daily_tier_limit_inr:
            side_effects.append(
                f"Exceeds tier velocity limit by INR {projected_24h_spend - self.daily_tier_limit_inr:,.2f}"
            )
        if net_amount_inr > self.instant_wire_threshold and irreversibility > 0.8:
            side_effects.append("Irreversible transfer above single-transaction threshold")

        return FinancialStateDelta(
            tool_name=tool_name,
            proposed_args=args,
            projected_net_delta_inr=net_amount_inr,
            rolling_24h_exposure_inr=projected_24h_spend,
            policy_tier_limit_inr=self.daily_tier_limit_inr,
            irreversibility_score=irreversibility,
            confidence=confidence,
            natural_key=natural_key,
            simulated_side_effects=side_effects,
        )

    def evaluate(
        self, delta: FinancialStateDelta, circuit_breaker: SteerCircuitBreaker
    ) -> EvaluationResult:
        if not delta.natural_key:
            return attach_evidence(
                EvaluationResult(
                    decision=GateDecision.ASK,
                    confidence=delta.confidence,
                    reason="Missing explicit natural key (claim_id, transaction_ref, or idempotency_key).",
                ),
                delta,
                self.notary,
            )

        if delta.confidence < MIN_CONFIDENCE_AUTOPASS:
            return attach_evidence(
                EvaluationResult(
                    decision=GateDecision.ASK,
                    confidence=delta.confidence,
                    reason="Low context-completeness score: missing KYC or fresh balance context.",
                ),
                delta,
                self.notary,
            )

        if (
            delta.rolling_24h_exposure_inr > (delta.policy_tier_limit_inr * 2.0)
            and delta.irreversibility_score >= 0.9
        ):
            return attach_evidence(
                EvaluationResult(
                    decision=GateDecision.DENY,
                    confidence=delta.confidence,
                    reason=(
                        f"Projected delta INR {delta.projected_net_delta_inr:,.2f} "
                        "critically breaches velocity envelope."
                    ),
                ),
                delta,
                self.notary,
            )

        if delta.rolling_24h_exposure_inr > delta.policy_tier_limit_inr:
            max_allowed_instant = max(
                0.0,
                delta.policy_tier_limit_inr
                - (delta.rolling_24h_exposure_inr - delta.projected_net_delta_inr),
            )
            base_steer = {
                "guidance": (
                    f"Cannot process full INR {delta.projected_net_delta_inr:,.2f} as instant disbursement. "
                    f"Option A: Process INR {max_allowed_instant:,.2f} instant + route remainder to dual-sign queue. "
                    f"Option B: Route entire claim to `submit_manual_review_ticket`."
                ),
                "suggested_tool": "create_staged_disbursement",
                "suggested_args": {
                    "immediate_amount": max_allowed_instant,
                    "escrow_amount": delta.projected_net_delta_inr - max_allowed_instant,
                },
            }
            return attach_evidence(
                circuit_breaker.resolve(
                    delta.natural_key, delta.proposed_args, delta.confidence, base_steer
                ),
                delta,
                self.notary,
            )

        return attach_evidence(
            EvaluationResult(
                decision=GateDecision.ALLOW,
                confidence=delta.confidence,
                reason="Projected balance delta is within safe autonomous operational boundary.",
            ),
            delta,
            self.notary,
        )
