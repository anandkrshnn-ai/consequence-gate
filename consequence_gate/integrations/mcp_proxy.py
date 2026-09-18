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
import threading
from collections.abc import Callable
from typing import Any

from ..core.approval import ApprovalDecision, AskCallback
from ..core.circuit_breaker import SteerCircuitBreaker
from ..core.evidence import ConsequenceNotary
from ..core.models import EvaluationResult, GateDecision
from ..core.store import Store
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
        store: Store | None = None,
        notary: ConsequenceNotary | None = None,
        ask_callback: AskCallback | None = None,
    ):
        """
        Args:
            downstream_command: Command to launch downstream MCP server
            simulator_fn: function(tool_name, args, context) -> delta
            evaluator_fn: function(delta, circuit_breaker) -> EvaluationResult
            circuit_breaker: SteerCircuitBreaker (default: max_retries=2)
            context_provider: function(request_params) -> context dict
            store: Optional Store for durable idempotency.
            notary: Optional ConsequenceNotary for evidence.
        """
        self.downstream_command = downstream_command
        self.simulator_fn = simulator_fn
        self.evaluator_fn = evaluator_fn
        self.circuit_breaker = circuit_breaker or SteerCircuitBreaker(max_retries=2, store=store)
        self.context_provider = context_provider or (lambda params: {})
        self.store = store
        self.notary = notary
        self.ask_callback = ask_callback

        self.downstream_process: subprocess.Popen | None = None
        self._downstream_lock = threading.Lock()

    def _intercept_tools_call(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """
        Intercept a tools/call request. Returns a response dict if the gate
        decides DENY/ASK/STEER, or None if the request should be forwarded.
        """
        params = request.get("params", {})
        tool_name = params.get("name", "unknown")
        arguments = params.get("arguments", {})
        context = self.context_provider(params)

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
            # If ASK and we have a callback, block on human approval
            if result.decision == GateDecision.ASK and self.ask_callback is not None:
                approval = self.ask_callback(result, result.evidence)
                if approval == ApprovalDecision.APPROVED:
                    return None  # Forward to downstream
                # Fall through to standard ESCALATION_REQUIRED error on REJECTED or TIMEOUT

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

    def _ensure_downstream(self) -> subprocess.Popen:
        """Launch the downstream MCP server subprocess if not already running."""
        if self.downstream_process is None:
            self.downstream_process = subprocess.Popen(
                self.downstream_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        return self.downstream_process

    def _forward_to_downstream(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Forward a JSON-RPC message to the downstream MCP server.

        Notification / request split:
        If the request has no "id" (a JSON-RPC notification, e.g.
        "initialized" or "cancelled"), it is written to the downstream
        and no response is read — notifications are fire-and-forget by
        the JSON-RPC 2.0 spec. If the request has an "id", a response is
        expected.

        Response correlation by JSON-RPC id:
        For requests, the downstream stdout is read line-by-line until a
        JSON-RPC response whose "id" matches the request's is found.
        Downstream server notifications (lines with no "id") are
        skipped — they are not the response to our request. This prevents
        a server-side log or progress notification from being
        misinterpreted as the response to a pending request.

        Single-flight lock (known limitation for v0.2):
        A threading.Lock ensures only one request/response cycle is in
        flight at a time. Concurrent callers serialize behind the lock.
        Pipelined requests from a client (two requests before either
        response) will block, not interleave responses. A full demux
        thread is deferred until a client actually pipelines.

        Raises:
            ConnectionError: if the downstream closes stdout before
            responding to a request.
        """
        is_notification = "id" not in request

        with self._downstream_lock:
            process = self._ensure_downstream()

            request_line = json.dumps(request) + "\n"
            process.stdin.write(request_line)
            process.stdin.flush()

            if is_notification:
                return None

            request_id = request["id"]
            while True:
                response_line = process.stdout.readline()
                if not response_line:
                    raise ConnectionError(
                        "Downstream MCP server closed stdout before responding "
                        f"to request id={request_id!r}"
                    )
                response = json.loads(response_line)
                # Skip notifications from downstream (no id) — not our response.
                if "id" not in response:
                    continue
                if response["id"] == request_id:
                    return response
                # A response with a different id: should not occur in
                # single-flight mode, but skip defensively and keep waiting.
                continue

    def _process_line(self, line: str) -> str | None:
        """Process a single JSON-RPC line from client."""
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            # Malformed JSON - forward as-is, let downstream handle
            return None

        method = request.get("method")
        if method != "tools/call":
            # Not a tool call — forward to downstream (initialize handshake,
            # resources/list, notifications, etc.). _forward_to_downstream
            # handles the notification vs request split internally.
            response = self._forward_to_downstream(request)
            return json.dumps(response) if response is not None else None

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
    store: Store | None = None,
    notary: ConsequenceNotary | None = None,
    ask_callback: AskCallback | None = None,
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
    predictor.notary = notary
    breaker = SteerCircuitBreaker(max_retries=max_retries, store=store)

    def evaluator(delta, circuit_breaker):
        return predictor.evaluate(delta, circuit_breaker)

    return MCPConsequenceProxy(
        downstream_command=downstream_command,
        simulator_fn=predictor.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
        store=store,
        notary=notary,
        ask_callback=ask_callback,
    )


def create_database_mcp_proxy(
    downstream_command: list,
    max_autonomous_delete_rows: int = 100,
    db_conn=None,
    max_retries: int = 2,
    context_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    store: Store | None = None,
    notary: ConsequenceNotary | None = None,
    ask_callback: AskCallback | None = None,
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
    simulator.notary = notary
    breaker = SteerCircuitBreaker(max_retries=max_retries, store=store)

    def evaluator(delta, circuit_breaker):
        return simulator.evaluate(delta, circuit_breaker)

    return MCPConsequenceProxy(
        downstream_command=downstream_command,
        simulator_fn=simulator.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
        store=store,
        notary=notary,
        ask_callback=ask_callback,
    )


def create_communications_mcp_proxy(
    downstream_command: list,
    max_autonomous_recipients: int = 10000,
    canary_min_size: int = 100,
    canary_max_bounce_rate: float = 0.05,
    canary_max_complaint_rate: float = 0.01,
    max_retries: int = 2,
    context_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    store: Store | None = None,
    notary: ConsequenceNotary | None = None,
    ask_callback: AskCallback | None = None,
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
    simulator.notary = notary
    breaker = SteerCircuitBreaker(max_retries=max_retries, store=store)

    def evaluator(delta, circuit_breaker):
        return simulator.evaluate(delta, circuit_breaker)

    return MCPConsequenceProxy(
        downstream_command=downstream_command,
        simulator_fn=simulator.simulate,
        evaluator_fn=evaluator,
        circuit_breaker=breaker,
        context_provider=context_provider,
        store=store,
        notary=notary,
        ask_callback=ask_callback,
    )
