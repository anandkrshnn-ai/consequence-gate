"""
Example: Run consequence-gate as an MCP proxy.

This script demonstrates how to run the MCP proxy in front of a downstream
MCP server. The proxy intercepts tools/call requests, runs consequence
simulation, and either allows, denies, asks, or steers the call.

Usage:
    # Financial disbursement gate
    python -m consequence_gate.integrations.examples.run_mcp_proxy financial \\
        --downstream-command "npx -y @modelcontextprotocol/server-postgres postgresql://localhost/mydb" \\
        --daily-tier-limit 25000 \\
        --instant-wire-threshold 10000

    # Database deletion gate
    python -m consequence_gate.integrations.examples.run_mcp_proxy database \\
        --downstream-command "npx -y @modelcontextprotocol/server-postgres postgresql://localhost/mydb" \\
        --max-autonomous-delete-rows 100

    # Communications blast gate
    python -m consequence_gate.integrations.examples.run_mcp_proxy communications \\
        --downstream-command "npx -y @modelcontextprotocol/server-sendgrid" \\
        --max-autonomous-recipients 10000
"""

import argparse
import sys

from consequence_gate.integrations.mcp_proxy import (
    create_communications_mcp_proxy,
    create_database_mcp_proxy,
    create_financial_mcp_proxy,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Run consequence-gate MCP proxy")
    subparsers = parser.add_subparsers(dest="domain", required=True)

    # Financial subcommand
    financial_parser = subparsers.add_parser("financial", help="Financial disbursement gate")
    financial_parser.add_argument(
        "--downstream-command", required=True, help="Downstream MCP server command"
    )
    financial_parser.add_argument("--daily-tier-limit", type=float, default=25000.0)
    financial_parser.add_argument("--instant-wire-threshold", type=float, default=10000.0)
    financial_parser.add_argument("--max-retries", type=int, default=2)

    # Database subcommand
    db_parser = subparsers.add_parser("database", help="Database deletion gate")
    db_parser.add_argument(
        "--downstream-command", required=True, help="Downstream MCP server command"
    )
    db_parser.add_argument("--max-autonomous-delete-rows", type=int, default=100)
    db_parser.add_argument("--max-retries", type=int, default=2)

    # Communications subcommand
    comm_parser = subparsers.add_parser("communications", help="Communications blast gate")
    comm_parser.add_argument(
        "--downstream-command", required=True, help="Downstream MCP server command"
    )
    comm_parser.add_argument("--max-autonomous-recipients", type=int, default=10000)
    comm_parser.add_argument("--canary-min-size", type=int, default=100)
    comm_parser.add_argument("--max-retries", type=int, default=2)

    return parser.parse_args()


def main():
    args = parse_args()

    downstream_command = args.downstream_command.split()

    if args.domain == "financial":
        proxy = create_financial_mcp_proxy(
            downstream_command=downstream_command,
            daily_tier_limit_inr=args.daily_tier_limit,
            instant_wire_threshold=args.instant_wire_threshold,
            max_retries=args.max_retries,
        )
    elif args.domain == "database":
        proxy = create_database_mcp_proxy(
            downstream_command=downstream_command,
            max_autonomous_delete_rows=args.max_autonomous_delete_rows,
            max_retries=args.max_retries,
        )
    elif args.domain == "communications":
        proxy = create_communications_mcp_proxy(
            downstream_command=downstream_command,
            max_autonomous_recipients=args.max_autonomous_recipients,
            canary_min_size=args.canary_min_size,
            max_retries=args.max_retries,
        )
    else:
        raise ValueError(f"Unknown domain: {args.domain}")

    print(f"Starting consequence-gate MCP proxy ({args.domain} domain)...", file=sys.stderr)
    print(f"Downstream command: {' '.join(downstream_command)}", file=sys.stderr)
    print("Reading from stdin, writing to stdout...", file=sys.stderr)

    proxy.run()


if __name__ == "__main__":
    main()
