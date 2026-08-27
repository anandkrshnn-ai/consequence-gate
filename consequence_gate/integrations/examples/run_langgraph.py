"""
Example: Use consequence-gate with LangGraph agents.

Two integration patterns:
1. Middleware (@wrap_tool_call) - for create_agent() workflows
2. Pre-tool-call node - for StateGraph workflows

Usage:
    # Pattern 1: Middleware with create_agent
    python -m consequence_gate.integrations.examples.run_langgraph middleware

    # Pattern 2: Node with StateGraph
    python -m consequence_gate.integrations.examples.run_langgraph node
"""

import argparse
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from langchain_core.messages import HumanMessage

from consequence_gate.integrations.langgraph_hook import create_financial_gate_middleware, create_financial_gate_node, AgentState


def run_middleware_example():
    """Example: Middleware pattern with create_agent."""
    from langchain.agents import create_agent
    from langchain_core.tools import tool

    @tool
    def process_claim(amount: float, claim_id: str, currency: str = "INR", payout_method: str = "standard_ach") -> str:
        """Process a claim payout."""
        return f"Processed claim {claim_id} for {amount} {currency} via {payout_method}"

    # Create middleware
    middleware = create_financial_gate_middleware(
        daily_tier_limit_inr=25000.0,
        instant_wire_threshold=10000.0,
        max_retries=2,
        context_provider=lambda state: {
            "account_rolling_24h_spend": 0.0,
            "kyc_verified": True,
        },
    )

    # Create agent with middleware
    agent = create_agent(
        model="claude-sonnet-4",
        tools=[process_claim],
        middleware=[middleware],
    )

    print("Middleware example:")
    print("Agent created with consequence-gate middleware")
    print("Try: agent.invoke({'messages': [('user', 'Process claim for 50,000 INR')]})")
    print()


def run_node_example():
    """Example: Pre-tool-call node with StateGraph."""
    from langgraph.graph import StateGraph, START, END
    from langchain_core.messages import AIMessage

    def agent_node(state: AgentState):
        """Mock agent node that proposes tool calls."""
        # In real usage, this would be an LLM call that proposes tool calls
        return {
            "messages": state.get("messages", []) + [AIMessage(content="I'll process this claim")],
            "tool_calls": [
                {
                    "name": "process_claim",
                    "args": {"amount": 50000, "claim_id": "c1", "currency": "INR", "payout_method": "instant_upi"},
                    "id": "call_1",
                }
            ],
        }

    def tool_execution_node(state: AgentState):
        """Mock tool execution node."""
        # In real usage, this would execute the actual tools
        tool_calls = state.get("tool_calls", [])
        tool_messages = []
        for tc in tool_calls:
            tool_messages.append(
                {
                    "type": "tool_result",
                    "name": tc["name"],
                    "content": f"Executed {tc['name']} with args {tc['args']}",
                }
            )
        return {"tool_messages": tool_messages}

    # Create gate node
    gate_node = create_financial_gate_node(
        daily_tier_limit_inr=25000.0,
        instant_wire_threshold=10000.0,
        max_retries=2,
        context_provider=lambda state: {
            "account_rolling_24h_spend": 0.0,
            "kyc_verified": True,
        },
    )

    # Build graph
    builder = StateGraph(AgentState)
    builder.add_node("consequence_gate", gate_node)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tool_execution_node)

    builder.add_edge(START, "agent")
    builder.add_edge("agent", "consequence_gate")
    builder.add_edge("consequence_gate", "tools")

    graph = builder.compile()

    print("Node example:")
    print("StateGraph created with consequence-gate pre-tool-call node")
    print("Try: graph.invoke({'messages': [HumanMessage(content='Process claim for 50,000 INR')]})")
    print()


def main():
    parser = argparse.ArgumentParser(description="Run consequence-gate LangGraph examples")
    parser.add_argument("pattern", choices=["middleware", "node"], help="Integration pattern to demonstrate")
    args = parser.parse_args()

    if args.pattern == "middleware":
        run_middleware_example()
    elif args.pattern == "node":
        run_node_example()
    else:
        raise ValueError(f"Unknown pattern: {args.pattern}")


if __name__ == "__main__":
    main()
