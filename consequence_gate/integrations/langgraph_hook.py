"""
LangGraph middleware node stub -- insert as a graph node before the tool
execution node, routing on GateDecision.
"""


def consequence_gate_node(state, evaluator, circuit_breaker, simulator):
    raise NotImplementedError("Wire this into your LangGraph graph as a pre-tool-call node.")
