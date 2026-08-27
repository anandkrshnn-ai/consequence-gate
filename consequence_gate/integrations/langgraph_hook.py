"""
LangGraph integration: middleware and pre-tool-call node for consequence gating.

Two integration patterns:
1. Middleware (@wrap_tool_call) - intercepts tool calls inside the agent graph
2. Pre-tool-call node - custom node inserted before the tool execution node

Both patterns support:
- ALLOW: tool executes normally
- DENY: raise exception or return error message in state
- ASK: interrupt() for human approval
- STEER: return structured guidance in state for agent to retry

LangGraph reference:
- Middleware: https://docs.langchain.com/oss/python/langchain/middleware/overview
- Interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- wrap_tool_call: https://reference.langchain.com/python/langchain/middleware
"""

from typing import Any, Callable, Dict, List, Optional, TypedDict
import json

from langchain_core.messages import ToolMessage, AIMessage
from langchain_core.tools import BaseTool

from ..core.models import GateDecision, EvaluationResult
from ..core.circuit_breaker import SteerCircuitBreaker
from ..simulators.financial import FinancialDeltaPredictor
from ..simulators.database import DataDeletionSimulator
from ..simulators.communications import OutboundCommunicationSimulator


# Pattern 1: Middleware with @wrap_tool_call

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
    def consequence_gate_wrapper(tool_call: dict, state: dict, tools: dict):
        """
        Wrap tool calls to run consequence simulation before execution.

        Args:
            tool_call: dict with "name", "args", "id", "type"="tool_call"
            state: current agent state dict
            tools: dict of tool_name -> BaseTool

        Returns:
            Modified tool_call dict, or raises exception for DENY/ASK,
            or returns guidance for STEER.
        """
        tool_name = tool_call.get("name", "unknown")
        arguments = tool_call.get("args", {})
        context = context_fn(state)

        # Extract natural key for idempotency
        natural_key = arguments.get("claim_id") or arguments.get("transaction_ref") or f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"

        # Run simulation + evaluation
        delta = simulator_fn(tool_name, arguments, context)
        result = evaluator_fn(delta, breaker)

        if result.decision == GateDecision.ALLOW:
            # Pass through - tool executes normally
            return tool_call

        if result.decision == GateDecision.DENY:
            # Hard block - raise exception that becomes a ToolMessage error
            raise ValueError(f"BLOCKED: {result.reason}")

        if result.decision == GateDecision.ASK:
            # Human approval required - use interrupt()
            from langgraph.graph import interrupt
            approval = interrupt(f"ESCALATION_REQUIRED: {result.reason}\\nApprove this tool call?")
            if not approval:
                raise ValueError("Human denied the tool call")
            return tool_call

        if result.decision == GateDecision.STEER:
            # Return structured guidance - agent sees this as tool result
            steer_payload = result.steer_payload or {}
            guidance = steer_payload.get("guidance", result.reason)
            suggested_tool = steer_payload.get("suggested_tool")
            suggested_args = steer_payload.get("suggested_args", {})
            idempotency_key = suggested_args.get("idempotency_key")
            if idempotency_key:
                guidance += f" [idempotency_key={idempotency_key}]"

            # Return a ToolMessage with isError-like content
            error_text = (
                f"STEER_GUIDANCE: {guidance}\\n"
                f"Suggested alternative: {suggested_tool} with args {suggested_args}"
            )
            return {
                "name": tool_name,
                "args": arguments,
                "id": tool_call.get("id"),
                "type": "tool_call",
                "_consequence_gate_result": {
                    "decision": "STEER",
                    "guidance": error_text,
                    "suggested_tool": suggested_tool,
                    "suggested_args": suggested_args,
                },
            }

        return tool_call

    return consequence_gate_wrapper


# Pattern 2: Pre-tool-call node for StateGraph

class AgentState(TypedDict, total=False):
    """Minimal agent state schema. Extend with your own fields."""
    messages: List
    tool_calls: List[dict]
    tool_messages: List[ToolMessage]
    consequence_gate_results: List[dict]


def create_consequence_gate_node(
    simulator_fn: Callable[[str, Dict[str, Any], Dict[str, Any]], Any],
    evaluator_fn: Callable[[Any, SteerCircuitBreaker], EvaluationResult],
    circuit_breaker: Optional[SteerCircuitBreaker] = None,
    context_provider: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
):
    """
    Factory for creating a pre-tool-call node for StateGraph.

    Usage:
        from langgraph.graph import StateGraph, START, END
        from consequence_gate.integrations.langgraph_hook import create_consequence_gate_node

        gate_node = create_consequence_gate_node(
            simulator_fn=financial_simulator,
            evaluator_fn=evaluator,
        )

        builder = StateGraph(AgentState)
        builder.add_node("consequence_gate", gate_node)
        builder.add_node("agent", agent_node)
        builder.add_edge(START, "consequence_gate")
        builder.add_edge("consequence_gate", "agent")
        graph = builder.compile()
    """
    from langgraph.graph import interrupt

    breaker = circuit_breaker or SteerCircuitBreaker(max_retries=2)
    context_fn = context_provider or (lambda state: {})

    def consequence_gate_node(state: AgentState) -> AgentState:
        """
        Pre-tool-call node that intercepts tool calls and applies consequence gating.

        Returns state updates with:
        - ALLOW: tool_calls pass through unchanged
        - DENY: tool_messages with error content
        - ASK: interrupt() for human approval
        - STEER: tool_messages with guidance content
        """
        tool_calls = state.get("tool_calls", [])
        if not tool_calls:
            return {}  # No tool calls to gate

        messages = state.get("messages", [])
        tool_messages = []
        gated_tool_calls = []
        gate_results = []

        for tool_call in tool_calls:
            tool_name = tool_call.get("name", "unknown")
            arguments = tool_call.get("args", {})
            context = context_fn(state)

            natural_key = arguments.get("claim_id") or arguments.get("transaction_ref") or f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"

            delta = simulator_fn(tool_name, arguments, context)
            result = evaluator_fn(delta, breaker)

            if result.decision == GateDecision.ALLOW:
                gated_tool_calls.append(tool_call)
                gate_results.append({"tool_call": tool_call, "decision": "ALLOW"})

            elif result.decision == GateDecision.DENY:
                error_msg = f"BLOCKED: {result.reason}"
                tool_msg = ToolMessage(content=error_msg, tool_call_id=tool_call.get("id", ""), name=tool_name, status="error")
                tool_messages.append(tool_msg)
                gate_results.append({"tool_call": tool_call, "decision": "DENY", "error": error_msg})

            elif result.decision == GateDecision.ASK:
                # Human approval required
                approval = interrupt(f"ESCALATION_REQUIRED: {result.reason}\\nApprove this tool call?")
                if approval:
                    gated_tool_calls.append(tool_call)
                    gate_results.append({"tool_call": tool_call, "decision": "ASK", "approved": True})
                else:
                    error_msg = "Human denied the tool call"
                    tool_msg = ToolMessage(content=error_msg, tool_call_id=tool_call.get("id", ""), name=tool_name, status="error")
                    tool_messages.append(tool_msg)
                    gate_results.append({"tool_call": tool_call, "decision": "ASK", "approved": False})

            elif result.decision == GateDecision.STEER:
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
                tool_msg = ToolMessage(content=error_text, tool_call_id=tool_call.get("id", ""), name=tool_name, status="error")
                tool_messages.append(tool_msg)
                gate_results.append({
                    "tool_call": tool_call,
                    "decision": "STEER",
                    "guidance": error_text,
                    "suggested_tool": suggested_tool,
                    "suggested_args": suggested_args,
                })

        return {
            "tool_calls": gated_tool_calls,
            "tool_messages": tool_messages,
            "consequence_gate_results": gate_results,
        }

    return consequence_gate_node


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


def create_financial_gate_node(
    daily_tier_limit_inr: float = 25000.0,
    instant_wire_threshold: float = 10000.0,
    max_retries: int = 2,
    context_provider: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
):
    """Factory for financial gate node."""
    predictor = FinancialDeltaPredictor(
        daily_tier_limit_inr=daily_tier_limit_inr,
        instant_wire_threshold=instant_wire_threshold,
    )
    breaker = SteerCircuitBreaker(max_retries=max_retries)

    def evaluator(delta, circuit_breaker):
        return predictor.evaluate(delta, circuit_breaker)

    return create_consequence_gate_node(
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


def create_database_gate_node(
    max_autonomous_delete_rows: int = 100,
    db_conn=None,
    max_retries: int = 2,
    context_provider: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
):
    """Factory for database gate node."""
    simulator = DataDeletionSimulator(
        max_autonomous_delete_rows=max_autonomous_delete_rows,
        db_conn=db_conn,
    )
    breaker = SteerCircuitBreaker(max_retries=max_retries)

    def evaluator(delta, circuit_breaker):
        return simulator.evaluate(delta, circuit_breaker)

    return create_consequence_gate_node(
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


def create_communications_gate_node(
    max_autonomous_recipients: int = 10000,
    canary_min_size: int = 100,
    canary_max_bounce_rate: float = 0.05,
    canary_max_complaint_rate: float = 0.01,
    max_retries: int = 2,
    context_provider: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
):
    """Factory for communications gate node."""
    simulator = OutboundCommunicationSimulator(
        max_autonomous_recipients=max_autonomous_recipients,
        canary_min_size=canary_min_size,
        canary_max_bounce_rate=canary_max_bounce_rate,
        canary_max_complaint_rate=canary_max_complaint_rate,
    )
    breaker = SteerCircuitBreaker(max_retries=max_retries)

    def evaluator(delta, circuit_breaker):
        return simulator.evaluate(delta, circuit_breaker)

    return create_consequence_gate_node(
        simulator_fn=simulator.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
    )
