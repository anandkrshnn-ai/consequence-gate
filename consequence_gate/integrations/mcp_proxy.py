"""
MCP Proxy: consequence-gate middleware for Model Context Protocol.

Intercepts tools/call requests, runs consequence simulation, and either:
- ALLOW: forwards request to downstream MCP server
- DENY: returns JSON-RPC error (code=-32603, "BLOCKED: <reason>")
- ASK: returns tool result with isError=true and "ESCALATION_REQUIRED" message
- STEER: returns tool result with isError=true and structured guidance

Transport: stdio (newline-delimited JSON-RPC)
- Reads from stdin (client -> proxy)
- Writes to stdout (proxy -> client)
- Forwards to downstream MCP server via subprocess stdio

MCP spec reference:
- tools/call: https://modelcontextprotocol.io/specification/2025-11-25/server/tools/
- Transport: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports/
- Error handling: https://apxml.com/courses/getting-started-model-context-protocol/chapter-3-implementing-tools-and-logic/error-handling-reporting
"""

import json
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from ..core.circuit_breaker import SteerCircuitBreaker
from ..core.models import EvaluationResult, GateDecision
from ..simulators.communications import OutboundCommunicationSimulator
from ..simulators.database import DataDeletionSimulator
from ..simulators.financial import FinancialDeltaPredictor


class MCPConsequenceProxy:
    """
    MCP proxy that sits between an MCP client (Claude Desktop, Cursor, etc.)
    and a downstream MCP server, intercepting tools/call requests to run
    consequence simulation before forwarding.

    Usage:
        proxy = MCPConsequenceProxy(
            downstream_command=["npx", "-y", "mcp-server-mytool"],
            simulator_fn=financial_simulator,
            evaluator_fn=evaluator,
            circuit_breaker=SteerCircuitBreaker(),
        )
        proxy.run()  # Blocks, reading from stdin, writing to stdout
    """

    def __init__(
        self,
        downstream_command: list,
        simulator_fn: Callable[[str, dict[str, Any], dict[str, Any]], Any],
        evaluator_fn: Callable[[Any, SteerCircuitBreaker], EvaluationResult],
        circuit_breaker: SteerCircuitBreaker | None = None,
        context_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        """
        Args:
            downstream_command: Command to launch downstream MCP server
            simulator_fn: function(tool_name, args, context) -> delta
            evaluator_fn: function(delta, circuit_breaker) -> EvaluationResult
            circuit_breaker: SteerCircuitBreaker (default: max_retries=2)
            context_provider: function(request_params) -> context dict
        """
        self.downstream_command = downstream_command
        self.simulator_fn = simulator_fn
        self.evaluator_fn = evaluator_fn
        self.circuit_breaker = circuit_breaker or SteerCircuitBreaker(max_retries=2)
        self.context_provider = context_provider or (lambda params: {})

        self.downstream_process: subprocess.Popen | None = None

    def _extract_natural_key(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Extract stable natural key for idempotency."""
        return (
            arguments.get("claim_id")
            or arguments.get("transaction_ref")
            or f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"
        )

    def _intercept_tools_call(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """
        Intercept a tools/call request. Returns a response dict if the gate
        decides DENY/ASK/STEER, or None if the request should be forwarded.
        """
        params = request.get("params", {})
        tool_name = params.get("name", "unknown")
        arguments = params.get("arguments", {})
        context = self.context_provider(params)

        self._extract_natural_key(tool_name, arguments)
        delta = self.simulator_fn(tool_name, arguments, context)
        result = self.evaluator_fn(delta, self.circuit_breaker)

        request_id = request.get("id")

        if result.decision == GateDecision.ALLOW:
            return None  # Forward to downstream

        if result.decision == GateDecision.DENY:
            # Protocol error - model cannot retry
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": f"BLOCKED: {result.reason}",
                },
            }

        if result.decision in (GateDecision.ASK, GateDecision.STEER):
            # Tool execution error - model can retry with adjusted parameters
            if result.decision == GateDecision.ASK:
                error_text = f"ESCALATION_REQUIRED: {result.reason}"
            else:  # STEER
                steer_payload = result.steer_payload or {}
                guidance = steer_payload.get("guidance", result.reason)
                suggested_tool = steer_payload.get("suggested_tool")
                suggested_args = steer_payload.get("suggested_args", {})
                idempotency_key = suggested_args.get("idempotency_key")
                if idempotency_key:
                    guidance += f" [idempotency_key={idempotency_key}]"
                error_text = (
                    f"STEER_GUIDANCE: {guidance}\n"
                    f"Suggested alternative: {suggested_tool} with args {suggested_args}"
                )

            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": error_text}],
                    "isError": True,
                },
            }

        return None  # Should not reach here

    def _forward_to_downstream(self, request: dict[str, Any]) -> dict[str, Any]:
        """Forward request to downstream MCP server and return response."""
        if self.downstream_process is None:
            self.downstream_process = subprocess.Popen(
                self.downstream_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

        # Write request to downstream stdin
        request_line = json.dumps(request) + "\n"
        self.downstream_process.stdin.write(request_line)
        self.downstream_process.stdin.flush()

        # Read response from downstream stdout
        response_line = self.downstream_process.stdout.readline()
        return json.loads(response_line)

    def _process_line(self, line: str) -> str | None:
        """Process a single JSON-RPC line from client."""
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            # Malformed JSON - forward as-is, let downstream handle
            return None

        method = request.get("method")
        if method != "tools/call":
            # Not a tool call - forward as-is
            return None

        # Intercept tools/call
        intercepted_response = self._intercept_tools_call(request)
        if intercepted_response is not None:
            # Gate decided - return response directly to client
            return json.dumps(intercepted_response)

        # Gate allowed - forward to downstream
        response = self._forward_to_downstream(request)
        return json.dumps(response)

    def run(self):
        """Main proxy loop: read from stdin, process, write to stdout."""
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue

                response_line = self._process_line(line)
                if response_line is not None:
                    sys.stdout.write(response_line + "\n")
                    sys.stdout.flush()
        except KeyboardInterrupt:
            pass
        finally:
            if self.downstream_process is not None:
                self.downstream_process.terminate()


# Convenience factory functions


def create_financial_mcp_proxy(
    downstream_command: list,
    daily_tier_limit_inr: float = 25000.0,
    instant_wire_threshold: float = 10000.0,
    max_retries: int = 2,
    context_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> MCPConsequenceProxy:
    """
    Factory for financial-disbursement MCP proxy.

    Usage:
        proxy = create_financial_mcp_proxy(
            downstream_command=["npx", "-y", "mcp-server-payments"],
            daily_tier_limit_inr=25000.0,
            context_provider=lambda params: {
                "account_rolling_24h_spend": get_spend(params),
                "kyc_verified": is_kyc_verified(params),
            },
        )
        proxy.run()
    """
    predictor = FinancialDeltaPredictor(
        daily_tier_limit_inr=daily_tier_limit_inr,
        instant_wire_threshold=instant_wire_threshold,
    )
    breaker = SteerCircuitBreaker(max_retries=max_retries)

    def evaluator(delta, circuit_breaker):
        return predictor.evaluate(delta, circuit_breaker)

    return MCPConsequenceProxy(
        downstream_command=downstream_command,
        simulator_fn=predictor.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
    )


def create_database_mcp_proxy(
    downstream_command: list,
    max_autonomous_delete_rows: int = 100,
    db_conn=None,
    max_retries: int = 2,
    context_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> MCPConsequenceProxy:
    """
    Factory for database-deletion MCP proxy.

    Usage:
        proxy = create_database_mcp_proxy(
            downstream_command=["npx", "-y", "mcp-server-postgres"],
            max_autonomous_delete_rows=100,
            db_conn=get_db_connection(),
        )
        proxy.run()
    """
    simulator = DataDeletionSimulator(
        max_autonomous_delete_rows=max_autonomous_delete_rows,
        db_conn=db_conn,
    )
    breaker = SteerCircuitBreaker(max_retries=max_retries)

    def evaluator(delta, circuit_breaker):
        return simulator.evaluate(delta, circuit_breaker)

    return MCPConsequenceProxy(
        downstream_command=downstream_command,
        simulator_fn=simulator.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
    )


def create_communications_mcp_proxy(
    downstream_command: list,
    max_autonomous_recipients: int = 10000,
    canary_min_size: int = 100,
    canary_max_bounce_rate: float = 0.05,
    canary_max_complaint_rate: float = 0.01,
    max_retries: int = 2,
    context_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> MCPConsequenceProxy:
    """
    Factory for communications-blast MCP proxy.

    Usage:
        proxy = create_communications_mcp_proxy(
            downstream_command=["npx", "-y", "mcp-server-sendgrid"],
            max_autonomous_recipients=10000,
            context_provider=lambda params: {
                "segment_counts": get_segments(params),
                "recent_unsubscribes": get_unsubscribes(params),
                "historical_bounce_rate": 0.02,
            },
        )
        proxy.run()
    """
    simulator = OutboundCommunicationSimulator(
        max_autonomous_recipients=max_autonomous_recipients,
        canary_min_size=canary_min_size,
        canary_max_bounce_rate=canary_max_bounce_rate,
        canary_max_complaint_rate=canary_max_complaint_rate,
    )
    breaker = SteerCircuitBreaker(max_retries=max_retries)

    def evaluator(delta, circuit_breaker):
        return simulator.evaluate(delta, circuit_breaker)

    return MCPConsequenceProxy(
        downstream_command=downstream_command,
        simulator_fn=simulator.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
    )
