"""
MCP middleware stub: intended to sit upstream of AgentWall-style MCP
proxies. Placeholder pending real MCP JSON-RPC wiring.
"""


class MCPConsequenceMiddleware:
    def __init__(self, evaluator, circuit_breaker):
        self.evaluator = evaluator
        self.circuit_breaker = circuit_breaker

    def intercept(self, tool_name, args, context):
        raise NotImplementedError("Wire this to your MCP transport layer.")
