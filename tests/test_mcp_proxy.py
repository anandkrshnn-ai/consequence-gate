"""Unit tests for MCPConsequenceProxy.

Tests cover:
- Non-tools/call requests (with id) are forwarded and response returned
- Notifications (no id) are forwarded without waiting for a response
- tools/call with ALLOW decision forwards to downstream
- tools/call with DENY decision returns JSON-RPC error
- tools/call with ASK decision returns tool result with isError=true
- tools/call with STEER decision returns structured guidance
- Idempotency token stability across retries
- Response correlation by JSON-RPC id (skips downstream notifications)
"""

import json
from unittest.mock import MagicMock, call

from consequence_gate.core.models import EvaluationResult, GateDecision
from consequence_gate.integrations.mcp_proxy import MCPConsequenceProxy, create_financial_mcp_proxy

def _make_proxy(**kwargs):
    """Build a proxy with no-op simulator/evaluator for forwarding tests."""
    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.9, numeric_deltas={}, irreversibility_score=0.0)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.ALLOW, confidence=0.9, reason="OK")

    defaults = dict(
        downstream_command=["echo", "test"],
        simulator_fn=simulator_fn,
        evaluator_fn=evaluator_fn,
    )
    defaults.update(kwargs)
    return MCPConsequenceProxy(**defaults)


# ---------------------------------------------------------------------------
# Non-tools/call forwarding
# ---------------------------------------------------------------------------

def test_non_tools_call_request_forwards_and_returns_response():
    """A non-tools/call request (with id) is forwarded and its response returned."""
    proxy = _make_proxy()
    expected = {"jsonrpc": "2.0", "id": 1, "result": {}}
    proxy._forward_to_downstream = MagicMock(return_value=expected)

    request = {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
    result = proxy._process_line(json.dumps(request))

    proxy._forward_to_downstream.assert_called_once_with(request)
    assert result == json.dumps(expected)


def test_notification_forwarded_no_response_expected():
    """A JSON-RPC notification (no id) is forwarded and None is returned."""
    proxy = _make_proxy()
    proxy._forward_to_downstream = MagicMock(return_value=None)

    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    result = proxy._process_line(json.dumps(notification))

    proxy._forward_to_downstream.assert_called_once_with(notification)
    assert result is None

def test_cancelled_notification_forwarded():
    """The 'cancelled' notification (no id) is forwarded, not swallowed."""
    proxy = _make_proxy()
    proxy._forward_to_downstream = MagicMock(return_value=None)

    notification = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": "abc-123"},
    }
    result = proxy._process_line(json.dumps(notification))

    proxy._forward_to_downstream.assert_called_once_with(notification)
    assert result is None


# ---------------------------------------------------------------------------
# tools/call interception
# ---------------------------------------------------------------------------

def test_tools_call_allowed_forwards():
    """tools/call with ALLOW decision forwards to downstream."""
    proxy = _make_proxy()
    proxy._forward_to_downstream = MagicMock(
        return_value={"jsonrpc": "2.0", "id": 1, "result": {"content": []}}
    )

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "test_tool", "arguments": {"amount": 100}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert response["id"] == 1
    proxy._forward_to_downstream.assert_called_once()

def test_tools_call_denied_returns_error():
    """tools/call with DENY decision returns JSON-RPC error."""
    proxy = _make_proxy(
        evaluator_fn=lambda delta, breaker: EvaluationResult(
            decision=GateDecision.DENY, confidence=0.9, reason="Critical breach"
        )
    )

    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "test_tool", "arguments": {}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert response["id"] == 2
    assert "error" in response
    assert response["error"]["code"] == -32603
    assert "BLOCKED: Critical breach" in response["error"]["message"]

def test_tools_call_ask_returns_iserror():
    """tools/call with ASK decision returns tool result with isError=true."""
    proxy = _make_proxy(
        evaluator_fn=lambda delta, breaker: EvaluationResult(
            decision=GateDecision.ASK, confidence=0.4, reason="Low confidence"
        )
    )

    request = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "test_tool", "arguments": {}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert response["id"] == 3
    assert response["result"]["isError"] is True
    assert "ESCALATION_REQUIRED: Low confidence" in response["result"]["content"][0]["text"]

def test_tools_call_steer_returns_guidance():
    """tools/call with STEER decision returns structured guidance."""
    proxy = _make_proxy(
        evaluator_fn=lambda delta, breaker: EvaluationResult(
            decision=GateDecision.STEER, confidence=0.9, reason="Exceeds tier limit",
            steer_payload={
                "guidance": "Reduce amount below 25000",
                "suggested_tool": "test_tool",
                "suggested_args": {
                    "amount": 20000,
                    "idempotency_key": "txn_123_steer",
                },
            },
        )
    )

    request = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "test_tool", "arguments": {"amount": 50000}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert response["id"] == 4
    assert response["result"]["isError"] is True
    error_text = response["result"]["content"][0]["text"]
    assert "STEER_GUIDANCE: Reduce amount below 25000" in error_text
    assert "[idempotency_key=txn_123_steer]" in error_text
    assert "amount" in error_text

def test_financial_factory_proxy():
    """Factory correctly wires up predictor, circuit breaker, and MCP proxy."""
    proxy = create_financial_mcp_proxy(
        downstream_command=["echo", "test"],
        daily_tier_limit_inr=25000.0,
        instant_wire_threshold=10000.0,
        max_retries=2,
        context_provider=lambda params: {
            "account_rolling_24h_spend": 0.0,
            "kyc_verified": True,
        },
    )

    assert isinstance(proxy, MCPConsequenceProxy)

    # Test an allowed call
    req_allow = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "disburse_funds", "arguments": {"amount": 5000, "transaction_ref": "txn_123"}},
    }
    # It will try to forward to downstream_command, we just mock the forwarder here
    proxy._forward_to_downstream = MagicMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
    proxy._process_line(json.dumps(req_allow))
    proxy._forward_to_downstream.assert_called_once()

    # Test a steered call (exceeds 25000 limit)
    req_steer = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "disburse_funds", "arguments": {"amount": 50000, "transaction_ref": "txn_456"}},
    }
    result_steer = proxy._process_line(json.dumps(req_steer))
    response = json.loads(result_steer)

    assert "result" in response
    assert response["result"]["isError"] is True
    assert "STEER_GUIDANCE:" in response["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# Response correlation by JSON-RPC id
# ---------------------------------------------------------------------------

def test_response_correlation_skips_downstream_notifications():
    """When reading a response, downstream notifications (no id) are skipped
    until a response with the matching id is found."""

    # Build a fake downstream process that emits a notification line
    # before the actual response.
    class FakeStream:
        def __init__(self, lines):
            self._lines = list(lines)
            self._idx = 0

        def readline(self):
            if self._idx >= len(self._lines):
                return ""
            line = self._lines[self._idx]
            self._idx += 1
            return line + "\n"

    class FakeProcess:
        def __init__(self):
            self.stdin = MagicMock()
            self.stdout = FakeStream([
                # Downstream emits a notification before the response
                json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 50}}),
                # The actual response with matching id
                json.dumps({"jsonrpc": "2.0", "id": 42, "result": {"content": [{"type": "text", "text": "done"}]}}),
            ])
            self.stderr = MagicMock()

    proxy = _make_proxy()
    proxy.downstream_process = FakeProcess()

    request = {"jsonrpc": "2.0", "id": 42, "method": "tools/call", "params": {"name": "x", "arguments": {}}}
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert response["id"] == 42
    assert response["result"]["content"][0]["text"] == "done"


def test_response_correlation_wrong_id_skipped():
    """A response with a different id is skipped, not returned."""

    class FakeStream:
        def __init__(self, lines):
            self._lines = list(lines)
            self._idx = 0

        def readline(self):
            if self._idx >= len(self._lines):
                return ""
            line = self._lines[self._idx]
            self._idx += 1
            return line + "\n"

    class FakeProcess:
        def __init__(self):
            self.stdin = MagicMock()
            self.stdout = FakeStream([
                # Stale response from a different request
                json.dumps({"jsonrpc": "2.0", "id": 999, "result": {}}),
                # Our actual response
                json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"content": []}}),
            ])
            self.stderr = MagicMock()

    proxy = _make_proxy()
    proxy.downstream_process = FakeProcess()

    request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "x", "arguments": {}}}
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert response["id"] == 7


def test_downstream_eof_raises_connection_error():
    """If the downstream closes stdout before responding, ConnectionError is raised."""

    class FakeStream:
        def readline(self):
            return ""  # EOF

    class FakeProcess:
        def __init__(self):
            self.stdin = MagicMock()
            self.stdout = FakeStream()
            self.stderr = MagicMock()

    proxy = _make_proxy()
    proxy.downstream_process = FakeProcess()

    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x", "arguments": {}}}

    try:
        proxy._process_line(json.dumps(request))
        assert False, "Should have raised ConnectionError"
    except ConnectionError:
        pass


# ---------------------------------------------------------------------------
# ASK Callback Integration
# ---------------------------------------------------------------------------

from consequence_gate.core.approval import ApprovalDecision

def test_ask_callback_approved_forwards():
    """If ASK callback returns APPROVED, the request is forwarded."""
    cb = MagicMock(return_value=ApprovalDecision.APPROVED)
    proxy = _make_proxy(
        evaluator_fn=lambda delta, breaker: EvaluationResult(
            decision=GateDecision.ASK, confidence=0.4, reason="Low confidence"
        ),
        ask_callback=cb,
    )
    proxy._forward_to_downstream = MagicMock(
        return_value={"jsonrpc": "2.0", "id": 5, "result": {"content": []}}
    )

    request = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "test_tool", "arguments": {}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert response["id"] == 5
    cb.assert_called_once()
    proxy._forward_to_downstream.assert_called_once()

def test_ask_callback_rejected_returns_iserror():
    """If ASK callback returns REJECTED, returns isError=True."""
    cb = MagicMock(return_value=ApprovalDecision.REJECTED)
    proxy = _make_proxy(
        evaluator_fn=lambda delta, breaker: EvaluationResult(
            decision=GateDecision.ASK, confidence=0.4, reason="Low confidence"
        ),
        ask_callback=cb,
    )

    request = {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {"name": "test_tool", "arguments": {}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert response["id"] == 6
    assert response["result"]["isError"] is True
    assert "ESCALATION_REQUIRED: Low confidence" in response["result"]["content"][0]["text"]
    cb.assert_called_once()

def test_ask_callback_timeout_returns_iserror():
    """If ASK callback returns TIMEOUT, returns isError=True."""
    cb = MagicMock(return_value=ApprovalDecision.TIMEOUT)
    proxy = _make_proxy(
        evaluator_fn=lambda delta, breaker: EvaluationResult(
            decision=GateDecision.ASK, confidence=0.4, reason="Low confidence"
        ),
        ask_callback=cb,
    )

    request = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "test_tool", "arguments": {}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert response["id"] == 7
    assert response["result"]["isError"] is True
    assert "ESCALATION_REQUIRED: Low confidence" in response["result"]["content"][0]["text"]
    cb.assert_called_once()

