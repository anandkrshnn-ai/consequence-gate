"""
Example: Use consequence-gate with LangGraph agents via middleware.

Usage:
    python -m consequence_gate.integrations.examples.run_langgraph
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from langchain.agents import create_agent
from langchain_core.tools import tool

from consequence_gate.integrations.langgraph_hook import create_financial_gate_middleware


@tool
def process_claim(amount: float, claim_id: str, currency: str = "INR", payout_method: str = "standard_ach") -> str:
    """Process a claim payout."""
    return f"Processed claim {claim_id} for {amount} {currency} via {payout_method}"


def main():
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

    print("LangGraph example:")
    print("Agent created with consequence-gate middleware")
    print()
    print("Try this invocation:")
    print('agent.invoke({"messages": [("user", "Process claim for 50,000 INR")]})')
    print()
    print("Expected behavior:")
    print("- The agent will propose a tool call: process_claim(amount=50000, ...)")
    print("- The middleware will intercept and simulate the outcome")
    print("- Since 50,000 INR exceeds the 25,000 INR tier limit, the middleware will return:")
    print('  ToolMessage(content="STEER_GUIDANCE: Cannot process full 50,000 INR...")')
    print("- The agent will see this as the tool result and can retry with a safer alternative")


if __name__ == "__main__":
    main()
