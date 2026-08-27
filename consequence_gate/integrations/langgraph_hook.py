"""
LangGraph integration: middleware that wraps tool calls with consequence gating.

Two patterns supported:
1. @wrap_tool_call decorator - intercepts tool execution in LangGraph agents
2. Custom ToolNode wrapper - for StateGraph workflows with explicit ToolNode

Reference:
- Agent Middleware: https://www.langchain.com/blog/agent-middleware
- wrap_tool_call: https://mcpservers.org/agent-skills/langchain-ai/langchain-middleware
- ToolNode: https://reference.langchain.com/python/langgraph.prebuilt/tool_node/ToolNode
"""

from typing import Any, Callable, Dict, Optional
import json

try:
    from langchain_core.messages import ToolMessage
    from langchain.tools.tool_node import ToolCallRequest
except ImportError:
    ToolMessage = None
    ToolCallRequest = None

from ..core.models import GateDecision, EvaluationResult
from ..core.circuit_breaker import SteerCircuitBreaker
from ..simulators.financial import FinancialDeltaPredictor
from ..simulators.database import DataDeletionSimulator
from ..simulators.communications import OutboundCommunicationSimulator


def create_consequence_middleware(
    simulator_fn: Callable[[str, Dict[str, Any], Dict[str, Any]], Any],
    evaluator_fn: Callable[[Any, SteerCircuitBreaker], EvaluationResult],
    circuit_breaker: Optional[SteerCircuitBreaker] = None,
    context_provider: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
):
    """
    Factory for creating a LangGraph middleware that wraps tool calls.

    Usage:
        from langchain.agents import create_agent
        from consequence_gate.integrations.langgraph_hook import create_consequence_middleware

        middleware = create_consequence_middleware(
            simulator_fn=financial_simulator,
            evaluator_fn=evaluator,
            circuit_breaker=SteerCircuitBreaker(),
        )

        agent = create_agent(
            model="claude-sonnet-4",
            tools=[my_tool],
            middleware=[middleware],
        )
    """
    from langchain.agents.middleware import wrap_tool_call

    breaker = circuit_breaker or SteerCircuitBreaker(max_retries=2)
    context_fn = context_provider or (lambda state: {})

    @wrap_tool_call
    def consequence_gate_wrapper(request: ToolCallRequest, handler):
        """
        Wrap tool calls to run consequence simulation before execution.

        Args:
            request: ToolCallRequest with tool_call dict and state
            handler: function to call to actually execute the tool

        Returns:
            Tool execution result, or raises exception for DENY,
            or returns ToolMessage with guidance for STEER.
        """
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown")
        arguments = tool_call.get("args", {})
        state = request.state or {}
        context = context_fn(state)

        # Extract natural key for idempotency
        natural_key = arguments.get("claim_id") or arguments.get("transaction_ref") or f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"

        # Run simulation + evaluation
        delta = simulator_fn(tool_name, arguments, context)
        result = evaluator_fn(delta, breaker)

        if result.decision == GateDecision.ALLOW:
            # Pass through - tool executes normally
            return handler(request)

        if result.decision == GateDecision.DENY:
            # Hard block - raise exception that becomes a ToolMessage error
            raise ValueError(f"BLOCKED: {result.reason}")

        if result.decision == GateDecision.ASK:
            # Human approval required - for now, raise exception with escalation message
            # In production, this would integrate with LangGraph's interrupt() or a human-in-the-loop system
            raise ValueError(f"ESCALATION_REQUIRED: {result.reason}")

        if result.decision == GateDecision.STEER:
            # Return ToolMessage with guidance - agent sees this as the tool result
            steer_payload = result.steer_payload or {}
            guidance = steer_payload.get("guidance", result.reason)
            suggested_tool = steer_payload.get("suggested_tool")
            suggested_args = steer_payload.get("suggested_args", {})
            idempotency_key = suggested_args.get("idempotency_key")
            if idempotency_key:
                guidance += f" [idempotency_key={idempotency_key}]"

            error_text = (
                f"STEER_GUIDANCE: {guidance}\\n"
                f"Suggested alternative: {suggested_tool} with args {suggested_args}"
            )
            return ToolMessage(content=error_text, tool_call_id=tool_call.get("id", ""), name=tool_name, status="error")

        # Should not reach here
        return handler(request)

    return consequence_gate_wrapper


# Convenience factory functions

def create_financial_gate_middleware(
    daily_tier_limit_inr: float = 25000.0,
    instant_wire_threshold: float = 10000.0,
    max_retries: int = 2,
    context_provider: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
):
    """Factory for financial gate middleware."""
    predictor = FinancialDeltaPredictor(
        daily_tier_limit_inr=daily_tier_limit_inr,
        instant_wire_threshold=instant_wire_threshold,
    )
    breaker = SteerCircuitBreaker(max_retries=max_retries)

    def evaluator(delta, circuit_breaker):
        return predictor.evaluate(delta, circuit_breaker)

    return create_consequence_middleware(
        simulator_fn=predictor.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
    )


def create_database_gate_middleware(
    max_autonomous_delete_rows: int = 100,
    db_conn=None,
    max_retries: int = 2,
    context_provider: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
):
    """Factory for database gate middleware."""
    simulator = DataDeletionSimulator(
        max_autonomous_delete_rows=max_autonomous_delete_rows,
        db_conn=db_conn,
    )
    breaker = SteerCircuitBreaker(max_retries=max_retries)

    def evaluator(delta, circuit_breaker):
        return simulator.evaluate(delta, circuit_breaker)

    return create_consequence_middleware(
        simulator_fn=simulator.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
    )


def create_communications_gate_middleware(
    max_autonomous_recipients: int = 10000,
    canary_min_size: int = 100,
    canary_max_bounce_rate: float = 0.05,
    canary_max_complaint_rate: float = 0.01,
    max_retries: int = 2,
    context_provider: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
):
    """Factory for communications gate middleware."""
    simulator = OutboundCommunicationSimulator(
        max_autonomous_recipients=max_autonomous_recipients,
        canary_min_size=canary_min_size,
        canary_max_bounce_rate=canary_max_bounce_rate,
        canary_max_complaint_rate=canary_max_complaint_rate,
    )
    breaker = SteerCircuitBreaker(max_retries=max_retries)

    def evaluator(delta, circuit_breaker):
        return simulator.evaluate(delta, circuit_breaker)

    return create_consequence_middleware(
        simulator_fn=simulator.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
    )
