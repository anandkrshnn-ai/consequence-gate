"""
Strands Agents integration: BeforeToolCallEvent hook adapter.

Strands API reference:
- BeforeToolCallEvent: https://strandsagents.com/docs/api/python/strands.hooks.events/
- Event attributes: selected_tool, cancel_tool (string or True)
- No event.set_result() exists — steering must be done via cancellation
  message that the agent sees on its next turn and can re-reason from.

Design contract (from project history):
- No silent argument mutation — we cancel with guidance, not rewrite args.
- Idempotency keys derived from natural_key, not regenerated per retry.
- Hard retry cap (default: 2) before forced escalation to ASK.
- Confidence-gated escalation — low confidence routes to ASK, never
  confident ALLOW/DENY on unfounded projections.
"""

from typing import Any, Callable, Dict, Optional
import json

from strands.hooks import BeforeToolCallEvent
from strands.hooks.events import HookProvider, HookRegistry

from ..core.models import GateDecision, EvaluationResult
from ..core.circuit_breaker import SteerCircuitBreaker
from ..simulators.financial import FinancialDeltaPredictor
from ..simulators.database import DataDeletionSimulator


class ConsequenceGateHook(HookProvider):
    """
    Strands hook provider that intercepts BeforeToolCallEvent, runs the
    consequence simulation gate, and either:
    - ALLOW: returns cleanly, tool executes normally
    - DENY: sets event.cancel_tool with a hard-block message
    - ASK: sets event.cancel_tool with an escalation message
    - STEER: sets event.cancel_tool with structured guidance that the
             agent can parse and retry toward on its next turn

    Usage:
        from consequence_gate.integrations.strands_hook import ConsequenceGateHook

        hook = ConsequenceGateHook(
            simulator_fn=financial_simulator,  # or database_simulator, etc.
            circuit_breaker=SteerCircuitBreaker(max_retries=2),
        )
        agent = Agent(hooks=[hook])
    """

    def __init__(
        self,
        simulator_fn: Callable[[str, Dict[str, Any], Dict[str, Any]], Any],
        evaluator_fn: Optional[Callable[[Any, SteerCircuitBreaker], EvaluationResult]] = None,
        circuit_breaker: Optional[SteerCircuitBreaker] = None,
        context_provider: Optional[Callable[[BeforeToolCallEvent], Dict[str, Any]]] = None,
    ):
        """
        Args:
            simulator_fn: function(tool_name, args, context) -> delta object
            evaluator_fn: function(delta, circuit_breaker) -> EvaluationResult
                          If None, uses a default generic evaluator.
            circuit_breaker: SteerCircuitBreaker instance (default: max_retries=2)
            context_provider: function(event) -> context dict for simulation
                              If None, uses a minimal default context.
        """
        self.simulator_fn = simulator_fn
        self.evaluator_fn = evaluator_fn
        self.circuit_breaker = circuit_breaker or SteerCircuitBreaker(max_retries=2)
        self.context_provider = context_provider or self._default_context

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.intercept)

    def _default_context(self, event: BeforeToolCallEvent) -> Dict[str, Any]:
        """
        Minimal default context extractor. Override this to pull in
        session state, user identity, account telemetry, etc.
        """
        return {
            "tool_use_id": event.tool_use.get("toolUseId"),
            "conversation_id": getattr(event, "invocation_state", {}).get("conversation_id"),
        }

    def intercept(self, event: BeforeToolCallEvent) -> None:
        tool_name = event.tool_use.get("name", "unknown")
        args = event.tool_use.get("input", {})
        context = self.context_provider(event)

        # Extract natural key from args (domain-specific; financial uses claim_id,
        # database uses table+filter hash, etc.)
        natural_key = args.get("claim_id") or args.get("transaction_ref") or f"{tool_name}:{json.dumps(args, sort_keys=True)}"

        # Run simulation + evaluation
        delta = self.simulator_fn(tool_name, args, context)

        if self.evaluator_fn is not None:
            result = self.evaluator_fn(delta, self.circuit_breaker)
        else:
            # Fallback: generic evaluator (not domain-aware, but safe)
            from ..core.evaluator import BlastRadiusEvaluator
            evaluator = BlastRadiusEvaluator()
            gate_decision = evaluator.evaluate(delta)
            # Wrap generic decision into EvaluationResult for uniform handling
            result = EvaluationResult(
                decision=gate_decision,
                confidence=delta.confidence,
                reason="Generic evaluator fallback.",
            )

        # Apply decision
        if result.decision == GateDecision.ALLOW:
            # Pass through — do nothing, tool executes normally
            return

        if result.decision == GateDecision.DENY:
            event.cancel_tool = f"BLOCKED: {result.reason}"
            return

        if result.decision == GateDecision.ASK:
            event.cancel_tool = f"ESCALATION_REQUIRED: {result.reason}"
            return

        if result.decision == GateDecision.STEER:
            # Cancel with structured guidance — agent sees this message
            # on its next turn and can re-reason toward the safer path.
            steer_payload = result.steer_payload or {}
            guidance_text = steer_payload.get("guidance", result.reason)
            suggested_tool = steer_payload.get("suggested_tool")
            suggested_args = steer_payload.get("suggested_args", {})

            # Include idempotency key in the guidance so the agent can
            # include it when retrying (if the suggested tool expects it).
            idempotency_key = suggested_args.get("idempotency_key")
            if idempotency_key:
                guidance_text += f" [idempotency_key={idempotency_key}]"

            cancel_message = (
                f"STEER_GUIDANCE: {guidance_text}\n"
                f"Suggested alternative: {suggested_tool} with args {suggested_args}"
            )
            event.cancel_tool = cancel_message
            return


# Convenience factory functions for common domain simulators

def create_financial_gate_hook(
    daily_tier_limit_inr: float = 25000.0,
    instant_wire_threshold: float = 10000.0,
    max_retries: int = 2,
    context_provider: Optional[Callable[[BeforeToolCallEvent], Dict[str, Any]]] = None,
) -> ConsequenceGateHook:
    """
    Factory for a financial-disbursement gate hook.

    Usage:
        hook = create_financial_gate_hook(
            daily_tier_limit_inr=25000.0,
            context_provider=lambda event: {
                "account_rolling_24h_spend": get_current_spend(event),
                "kyc_verified": is_kyc_verified(event),
            },
        )
        agent = Agent(hooks=[hook])
    """
    predictor = FinancialDeltaPredictor(
        daily_tier_limit_inr=daily_tier_limit_inr,
        instant_wire_threshold=instant_wire_threshold,
    )
    breaker = SteerCircuitBreaker(max_retries=max_retries)

    def evaluator(delta, circuit_breaker):
        return predictor.evaluate(delta, circuit_breaker)

    return ConsequenceGateHook(
        simulator_fn=predictor.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
    )


def create_database_gate_hook(
    max_autonomous_delete_rows: int = 100,
    db_conn=None,
    max_retries: int = 2,
    context_provider: Optional[Callable[[BeforeToolCallEvent], Dict[str, Any]]] = None,
) -> ConsequenceGateHook:
    """
    Factory for a database-deletion gate hook.

    Usage:
        hook = create_database_gate_hook(
            max_autonomous_delete_rows=100,
            db_conn=get_db_connection(),
            context_provider=lambda event: {
                "table_metadata": get_table_metadata(event),
            },
        )
        agent = Agent(hooks=[hook])
    """
    simulator = DataDeletionSimulator(
        max_autonomous_delete_rows=max_autonomous_delete_rows,
        db_conn=db_conn,
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
