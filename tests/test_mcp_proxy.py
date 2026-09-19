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
from unittest.mock import MagicMock

from consequence_gate.core.approval import ApprovalDecision
from consequence_gate.core.models import EvaluationResult, GateDecision
from consequence_gate.integrations.mcp_proxy import MCPConsequenceProxy, create_financial_mcp_proxy


def _make_proxy(**kwargs):
    """Build a proxy with no-op simulator/evaluator for forwarding tests."""

    def simulator_fn(tool_name, args, context):
        return MagicMock(confidence=0.9, numeric_deltas={}, irreversibility_score=0.0)

    def evaluator_fn(delta, breaker):
        return EvaluationResult(decision=GateDecision.ALLOW, confidence=0.9, reason="OK")

    defaults = {
        "downstream_command": ["echo", "test"],
        "simulator_fn": simulator_fn,
        "evaluator_fn": evaluator_fn,
    }
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
            decision=GateDecision.STEER,
            confidence=0.9,
            reason="Exceeds tier limit",
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
        "params": {
            "name": "disburse_funds",
            "arguments": {"amount": 5000, "transaction_ref": "txn_123"},
        },
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
        "params": {
            "name": "disburse_funds",
            "arguments": {"amount": 50000, "transaction_ref": "txn_456"},
        },
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
    import queue

    proxy = _make_proxy()
    proxy.downstream_process = MagicMock()
    proxy._stdout_queue = queue.Queue()

    proxy._stdout_queue.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progress": 50},
            }
        )
    )
    proxy._stdout_queue.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "result": {"content": [{"type": "text", "text": "done"}]},
            }
        )
    )

    request = {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "tools/call",
        "params": {"name": "x", "arguments": {}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert response["id"] == 42
    assert response["result"]["content"][0]["text"] == "done"


def test_response_correlation_wrong_id_skipped():
    """A response with a different id is skipped, not returned."""
    import queue

    proxy = _make_proxy()
    proxy.downstream_process = MagicMock()
    proxy._stdout_queue = queue.Queue()
    proxy._stdout_queue.put(json.dumps({"jsonrpc": "2.0", "id": 999, "result": {}}))
    proxy._stdout_queue.put(json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"content": []}}))

    request = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "x", "arguments": {}},
    }
    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)

    assert response["id"] == 7


def test_downstream_eof_returns_error():
    """If the downstream closes stdout before responding, returns a JSON-RPC error."""
    import queue

    proxy = _make_proxy()
    proxy.downstream_process = MagicMock()
    proxy._stdout_queue = queue.Queue()
    proxy._stdout_queue.put(None)  # EOF

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "x", "arguments": {}},
    }

    result = proxy._process_line(json.dumps(request))
    response = json.loads(result)
    assert response["id"] == 1
    assert response["error"]["code"] == -32000
    assert "process is dead" in response["error"]["message"]


# ---------------------------------------------------------------------------
# ASK Callback Integration
# ---------------------------------------------------------------------------


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


def test_downstream_process_death_fails_fast():
    """If the downstream process dies, the next request should fail fast rather than timing out."""
    import sys
    import threading

    proxy = _make_proxy(
        # A process that stays alive until we kill it
        downstream_command=[sys.executable, "-c", "import time; time.sleep(10)"],
        downstream_timeout=2.0,
    )

    # 1. First request starts the process
    request1 = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "x", "arguments": {}},
    }

    result_container = {}

    def run_req1():
        try:
            res = proxy._process_line(json.dumps(request1))
            result_container["req1"] = res
        except Exception as e:
            result_container["req1_error"] = e

    t1 = threading.Thread(target=run_req1)
    t1.start()

    # Give it a moment to ensure downstream is spawned
    import time

    time.sleep(0.5)

    # Kill the downstream process!
    proxy.downstream_process.terminate()
    proxy.downstream_process.wait()

    t1.join(timeout=3.0)

    # The first request should get a -32000 error, NOT crash
    assert "req1_error" not in result_container, (
        f"Unexpected error: {result_container.get('req1_error')}"
    )
    res1 = json.loads(result_container["req1"])
    assert res1["error"]["code"] == -32000
    assert "process is dead" in res1["error"]["message"]

    # 2. Second request should hit the fast-fail _downstream_dead logic
    request2 = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "x", "arguments": {}},
    }

    start_time = time.time()
    result2 = proxy._process_line(json.dumps(request2))
    elapsed = time.time() - start_time

    # It should fail fast (well under the 2.0s timeout)
    assert elapsed < 0.5

    response2 = json.loads(result2)
    assert response2["error"]["code"] == -32000
    assert "process is dead" in response2["error"]["message"]


def test_downstream_write_broken_pipe():
    """If process.stdin.write raises BrokenPipeError, it returns a -32000 error without crashing."""
    proxy = _make_proxy()
    proxy._downstream_dead = False
    
    mock_process = MagicMock()
    mock_process.stdin.write.side_effect = BrokenPipeError("Broken pipe")
    proxy._ensure_downstream = MagicMock(return_value=mock_process)

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "x", "arguments": {}},
    }
    
    result = proxy._process_line(json.dumps(request))
    assert result is not None
    response = json.loads(result)
    
    assert response["id"] == 1
    assert response["error"]["code"] == -32000
    assert "process is dead" in response["error"]["message"]
    assert proxy._downstream_dead is True
