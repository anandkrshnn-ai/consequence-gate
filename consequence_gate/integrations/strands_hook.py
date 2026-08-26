"""
AWS Strands BeforeToolCallEvent adapter stub.
See: Strands hook lifecycle -- event.cancel_tool for hard block,
this layer runs BEFORE that to decide whether to cancel, steer, or pass through.
"""


def before_tool_call_gate(event, evaluator, circuit_breaker, simulator):
    raise NotImplementedError("Wire this to strands.hooks.BeforeToolCallEvent.")
