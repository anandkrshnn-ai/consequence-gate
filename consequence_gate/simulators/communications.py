"""
OutboundCommunicationSimulator: outcome simulator for email/SMS/notification blasts.

Models:
- Recipient blast radius (total recipients, segment breakdown)
- Unsubscribe suppression check (cross-references recent unsubscribes)
- Canary cohort analysis (sends to small cohort first, measures bounce/complaint rate)
- Reputation risk scoring (sender domain reputation impact based on volume + content type)
- Irreversibility: once sent, email/SMS cannot be recalled (1.0); in-app notifications can be retracted (0.3)

Unlike financial/database simulators, this domain has no "partial execution" state —
a send is atomic and irreversible. The gate's job is to prevent oversends before they
happen, not to model intermediate states.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum

from ..core.models import GateDecision, EvaluationResult
from ..core.circuit_breaker import SteerCircuitBreaker


class CommunicationChannel(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push_notification"
    IN_APP = "in_app_notification"


@dataclass
class CommunicationBlastDelta:
    tool_name: str
    channel: str
    total_recipients: int
    segment_breakdown: Dict[str, int]
    has_unsubscribe_suppression: bool
    canary_cohort_size: int
    predicted_bounce_rate: float
    predicted_complaint_rate: float
    sender_reputation_impact: float
    irreversibility_score: float
    confidence: float
    natural_key: str
    simulated_side_effects: List[str] = field(default_factory=list)


class OutboundCommunicationSimulator:
    """
    Simulates outbound communication blast radius and reputation risk.
    
    Key design decisions:
    - No partial execution modeling — sends are atomic and irreversible.
    - Unsubscribe suppression is mandatory for compliance (CAN-SPAM, GDPR).
    - Canary cohorts are optional but recommended for high-volume sends.
    - Reputation impact is modeled as a function of volume + predicted complaint rate.
    """

    def __init__(
        self,
        max_autonomous_recipients: int = 10000,
        max_daily_send_limit: int = 100000,
        canary_min_size: int = 100,
        canary_max_bounce_rate: float = 0.05,
        canary_max_complaint_rate: float = 0.01,
    ):
        self.max_autonomous_recipients = max_autonomous_recipients
        self.max_daily_send_limit = max_daily_send_limit
        self.canary_min_size = canary_min_size
        self.canary_max_bounce_rate = canary_max_bounce_rate
        self.canary_max_complaint_rate = canary_max_complaint_rate

    def simulate(
        self,
        tool_name: str,
        args: Dict[str, Any],
        context: Dict[str, Any],
    ) -> CommunicationBlastDelta:
        channel = args.get("channel", "email").lower()
        recipient_list = args.get("recipients", [])
        segment_filter = args.get("segment_filter")
        suppress_unsubscribes = args.get("suppress_unsubscribes", True)
        canary_enabled = args.get("canary_enabled", False)
        canary_size = args.get("canary_size", self.canary_min_size)
        
        natural_key = f"{channel}:{args.get('campaign_id') or len(recipient_list)}"

        # Calculate total recipients
        if recipient_list:
            total_recipients = len(recipient_list)
        elif segment_filter and "segment_counts" in context:
            total_recipients = context["segment_counts"].get(segment_filter, 0)
        else:
            total_recipients = 0

        # Segment breakdown
        segment_breakdown = {}
        if "segment_counts" in context and segment_filter:
            segment_breakdown = {segment_filter: total_recipients}
        elif "segment_counts" in context:
            segment_breakdown = context["segment_counts"]
        else:
            segment_breakdown = {"unknown": total_recipients}

        # Unsubscribe suppression check
        recent_unsubscribes = context.get("recent_unsubscribes", set())
        has_suppression = suppress_unsubscribes and len(recent_unsubscribes) > 0
        recipients_without_suppression = total_recipients if not has_suppression else 0

        # Canary cohort analysis
        if canary_enabled and total_recipients > self.canary_min_size:
            historical_bounce_rate = context.get("historical_bounce_rate", 0.02)
            historical_complaint_rate = context.get("historical_complaint_rate", 0.005)
            predicted_bounce = historical_bounce_rate
            predicted_complaint = historical_complaint_rate
        else:
            predicted_bounce = 0.0
            predicted_complaint = 0.0

        # Sender reputation impact
        reputation_impact = -(total_recipients * predicted_complaint) / 10000.0
        if channel == "sms":
            reputation_impact *= 1.5

        # Irreversibility scoring
        if channel in ("email", "sms"):
            irreversibility = 1.0
        elif channel == "push_notification":
            irreversibility = 0.8
        else:
            irreversibility = 0.3

        # Confidence scoring
        has_segment_data = "segment_counts" in context
        has_historical_data = "historical_bounce_rate" in context
        confidence = 0.90 if (has_segment_data and has_historical_data) else 0.50

        side_effects = []
        if total_recipients > self.max_autonomous_recipients:
            side_effects.append(
                f"Recipient count ({total_recipients}) exceeds autonomous threshold "
                f"({self.max_autonomous_recipients})"
            )
        if not suppress_unsubscribes and total_recipients > 0:
            side_effects.append(
                f"COMPLIANCE RISK: {recipients_without_suppression} recipients without unsubscribe suppression"
            )
        if canary_enabled and predicted_bounce > self.canary_max_bounce_rate:
            side_effects.append(
                f"Canary predicts bounce rate {predicted_bounce:.1%} exceeds threshold {self.canary_max_bounce_rate:.1%}"
            )
        if canary_enabled and predicted_complaint > self.canary_max_complaint_rate:
            side_effects.append(
                f"Canary predicts complaint rate {predicted_complaint:.2%} exceeds threshold {self.canary_max_complaint_rate:.1%}"
            )
        if reputation_impact < -0.5:
            side_effects.append(
                f"Severe sender reputation risk: {reputation_impact:.2f} impact score"
            )

        return CommunicationBlastDelta(
            tool_name=tool_name,
            channel=channel,
            total_recipients=total_recipients,
            segment_breakdown=segment_breakdown,
            has_unsubscribe_suppression=has_suppression,
            canary_cohort_size=canary_size if canary_enabled else 0,
            predicted_bounce_rate=predicted_bounce,
            predicted_complaint_rate=predicted_complaint,
            sender_reputation_impact=reputation_impact,
            irreversibility_score=irreversibility,
            confidence=confidence,
            natural_key=natural_key,
            simulated_side_effects=side_effects,
        )

    def evaluate(
        self,
        delta: CommunicationBlastDelta,
        circuit_breaker: SteerCircuitBreaker,
    ) -> EvaluationResult:
        if delta.confidence < 0.60:
            return EvaluationResult(
                decision=GateDecision.ASK,
                confidence=delta.confidence,
                reason="Insufficient context (missing segment or historical data) to evaluate send safely.",
            )

        if not delta.has_unsubscribe_suppression and delta.total_recipients > 0:
            return EvaluationResult(
                decision=GateDecision.DENY,
                confidence=delta.confidence,
                reason=f"COMPLIANCE VIOLATION: Cannot send to {delta.total_recipients} recipients without unsubscribe suppression (CAN-SPAM/GDPR).",
            )

        if delta.sender_reputation_impact < -1.0:
            return EvaluationResult(
                decision=GateDecision.DENY,
                confidence=delta.confidence,
                reason=f"Critical sender reputation risk: {delta.sender_reputation_impact:.2f} impact score would severely damage deliverability.",
            )

        if delta.total_recipients > self.max_autonomous_recipients:
            canary_size = min(self.canary_min_size, delta.total_recipients // 10)
            base_steer = {
                "guidance": (
                    f"Direct send to {delta.total_recipients} recipients exceeds autonomous threshold "
                    f"({self.max_autonomous_recipients}). Option A: Run canary cohort of {canary_size} recipients first. "
                    f"Option B: Stage send in batches of {self.max_autonomous_recipients} with manual approval between batches."
                ),
                "suggested_tool": "send_canary_cohort",
                "suggested_args": {
                    "channel": delta.channel,
                    "recipients": f"canary_cohort_{canary_size}",
                    "canary_enabled": True,
                    "canary_size": canary_size,
                    "suppress_unsubscribes": True,
                    "idempotency_key": None,
                },
            }
            return circuit_breaker.resolve(delta.natural_key, delta.confidence, base_steer)

        if delta.canary_cohort_size > 0 and (
            delta.predicted_bounce_rate > self.canary_max_bounce_rate
            or delta.predicted_complaint_rate > self.canary_max_complaint_rate
        ):
            base_steer = {
                "guidance": (
                    f"Canary cohort predicts {delta.predicted_bounce_rate:.1%} bounce rate and "
                    f"{delta.predicted_complaint_rate:.2%} complaint rate, exceeding safe thresholds. "
                    f"Recommend: Run list hygiene (remove hard bounces, re-engagement campaign) before full send."
                ),
                "suggested_tool": "run_list_hygiene",
                "suggested_args": {
                    "segment": list(delta.segment_breakdown.keys())[0],
                    "remove_hard_bounces": True,
                    "reengagement_threshold_days": 90,
                },
            }
            return circuit_breaker.resolve(delta.natural_key, delta.confidence, base_steer)

        return EvaluationResult(
            decision=GateDecision.ALLOW,
            confidence=delta.confidence,
            reason=f"Send to {delta.total_recipients} recipients is within safe autonomous operational boundary.",
        )


def create_communication_gate_hook(
    max_autonomous_recipients: int = 10000,
    max_daily_send_limit: int = 100000,
    canary_min_size: int = 100,
    canary_max_bounce_rate: float = 0.05,
    canary_max_complaint_rate: float = 0.01,
    max_retries: int = 2,
    context_provider=None,
):
    """
    Factory for creating a communication gate simulator.
    
    Usage:
        simulator = create_communication_gate_hook(
            max_autonomous_recipients=10000,
            context_provider=lambda event: {
                "segment_counts": get_segment_counts(event),
                "recent_unsubscribes": get_recent_unsubscribes(event),
                "historical_bounce_rate": 0.02,
                "historical_complaint_rate": 0.005,
            },
        )
    """
    from ..integrations.strands_hook import ConsequenceGateHook
    
    simulator = OutboundCommunicationSimulator(
        max_autonomous_recipients=max_autonomous_recipients,
        max_daily_send_limit=max_daily_send_limit,
        canary_min_size=canary_min_size,
        canary_max_bounce_rate=canary_max_bounce_rate,
        canary_max_complaint_rate=canary_max_complaint_rate,
    )
    breaker = SteerCircuitBreaker(max_retries=max_retries)

    def evaluator(delta, circuit_breaker):
        return simulator.evaluate(delta, circuit_breaker)

    return ConsequenceGateHook(
        simulator_fn=simulator.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
    )
