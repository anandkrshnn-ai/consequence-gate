# consequence-gate

A speculative outcome-simulation layer for AI agent tool calls. It sits
**upstream** of static runtime access gates (AgentWall, AWS Strands
`BeforeToolCallEvent`, MCP proxies, Prisma AIRS) and asks a different
question than they do.

Static gates ask: *does this call match an allowed pattern?*
`consequence-gate` asks: *what will this call actually do, and is that
outcome safe?*

## Why this exists

Static runtime gates are fast (sub-millisecond) and effective at schema
validation, RBAC, and pattern matching -- but a schema-valid,
policy-compliant call can still be consequence-catastrophic. A
`process_claim(amount=50000)` call can pass every static check while
pushing an account over its daily velocity limit via an irreversible
instant transfer. `consequence-gate` projects the *outcome* of a call
(balance deltas, row-count blast radius, FK cascade depth,
irreversibility) before the call reaches your existing static gate, and
either passes it through, asks a human, denies it outright, or steers
the agent toward a pre-vetted safer alternative.

This is explicitly **not** a replacement for AgentWall / Strands / MCP
proxies -- it's a prediction layer that runs before them, in the same
pipeline.

## Core contracts

- **No silent argument mutation.** Steering returns structured guidance
  and a suggested alternative call; the agent (or a human) still has to
  commit to it. This preserves the audit property that every executed
  call was one the agent explicitly chose.
- **Idempotency keys are derived from the transaction's own natural key**
  (e.g. `claim_id`, or `table + filter hash`), never a fresh random token
  per retry -- otherwise a lost-response retry looks like a brand-new
  transaction instead of a duplicate.
- **Hard retry cap on STEER.** Regardless of guidance quality, retries
  are capped (default: 2) before forcing escalation to a human, as a
  backstop against loop-thrashing.
- **Confidence-gated escalation.** Low-confidence projections route to
  `ASK`, never to a confident-looking `ALLOW` or `DENY` -- an
  unfounded heuristic is worse than admitting uncertainty.

## Modules

- `consequence_gate.simulators.financial` -- **functional**: disbursement / claim / refund
  velocity and irreversibility modeling.
- `consequence_gate.simulators.database` -- **functional**: row-count blast radius via the
  DB's own query planner (`EXPLAIN`, not hardcoded selectivity constants)
  and recursive `ON DELETE CASCADE` graph walking.
- `consequence_gate.simulators.communications` -- **functional**: outbound email/SMS/notification
  blast radius, unsubscribe suppression compliance, canary cohort analysis, sender reputation impact.
- `consequence_gate.core` -- shared models, the confidence/threshold
  evaluator, and the idempotency-locked circuit breaker.
- `consequence_gate.integrations.strands_hook` -- **functional**: AWS Strands
  `BeforeToolCallEvent` adapter with full `ALLOW`/`DENY`/`ASK`/`STEER` lifecycle.
- `consequence_gate.integrations.mcp_proxy` -- **functional**: MCP stdio proxy
  intercepting `tools/call` requests, returning JSON-RPC errors or `isError=true` tool results.
- `consequence_gate.integrations.langgraph_hook` -- **functional**: LangGraph middleware
  (`@wrap_tool_call`) intercepting tool execution with full `ALLOW`/`DENY`/`ASK`/`STEER` lifecycle.
- `consequence_gate.backtest` -- offline JSONL trace replay harness and
  four-quadrant FP/FN/TN report generator, for evaluating this layer
  against historical execution logs with zero production integration.

## Quickstart

### Installation

```bash
pip install -e ".[dev]"
pytest
python examples/run_backtest_demo.py
```

### AWS Strands Integration

```python
from consequence_gate.integrations.strands_hook import create_financial_gate_hook
from strands.agents import Agent

hook = create_financial_gate_hook(
    daily_tier_limit_inr=25000.0,
    instant_wire_threshold=10000.0,
    max_retries=2,
    context_provider=lambda event: {
        "account_rolling_24h_spend": get_current_spend(event),
        "kyc_verified": is_kyc_verified(event),
    },
)

agent = Agent(hooks=[hook])
response = agent("Process this claim for 50,000 INR")
```

### MCP Proxy Integration

Run as a standalone proxy in front of any MCP server:

```bash
# Financial disbursement gate
python -m consequence_gate.integrations.examples.run_mcp_proxy financial \\
    --downstream-command "npx -y @modelcontextprotocol/server-postgres postgresql://localhost/mydb" \\
    --daily-tier-limit 25000 \\
    --instant-wire-threshold 10000
```

Configure in Claude Desktop / Cursor / Windsurf:

```json
{
  "mcpServers": {
    "my-consequence-gate": {
      "command": "python",
      "args": ["-m", "consequence_gate.integrations.examples.run_mcp_proxy", "financial", "--downstream-command", "npx -y @modelcontextprotocol/server-postgres postgresql://localhost/mydb"]
    }
  }
}
```

### LangGraph Integration

```python
from langchain.agents import create_agent
from consequence_gate.integrations.langgraph_hook import create_financial_gate_middleware

middleware = create_financial_gate_middleware(
    daily_tier_limit_inr=25000.0,
    instant_wire_threshold=10000.0,
    max_retries=2,
)

agent = create_agent(
    model="claude-sonnet-4",
    tools=[my_tool],
    middleware=[middleware],
)
```

Run the example:

```bash
python -m consequence_gate.integrations.examples.run_langgraph
```

### Database Deletion Gate (Strands)

```python
from consequence_gate.integrations.strands_hook import create_database_gate_hook

hook = create_database_gate_hook(
    max_autonomous_delete_rows=100,
    db_conn=get_db_connection(),
    max_retries=2,
    context_provider=lambda event: {
        "table_metadata": get_table_metadata(event),
    },
)

agent = Agent(hooks=[hook])
```

### Communications Blast Gate (Strands)

```python
from consequence_gate.integrations.strands_hook import create_communication_gate_hook

hook = create_communication_gate_hook(
    max_autonomous_recipients=10000,
    canary_min_size=100,
    canary_max_bounce_rate=0.05,
    canary_max_complaint_rate=0.01,
    context_provider=lambda event: {
        "segment_counts": get_segment_counts(event),
        "recent_unsubscribes": get_recent_unsubscribes(event),
        "historical_bounce_rate": 0.02,
        "historical_complaint_rate": 0.005,
    },
)

agent = Agent(hooks=[hook])
```

## Decision Matrix

| Decision | Strands | MCP | LangGraph |
|----------|---------|-----|-----------|
| `ALLOW` | Executes normally | Forwarded to downstream MCP server | Tool executes via handler(request) |
| `DENY` | `BLOCKED: <reason>` | JSON-RPC error (code=-32603) | Raises `ValueError("BLOCKED: ...")` |
| `ASK` | `ESCALATION_REQUIRED: <reason>` | `isError=true` tool result | Raises `ValueError("ESCALATION_REQUIRED: ...")` |
| `STEER` | `STEER_GUIDANCE: <guidance>\nSuggested alternative...` | `isError=true` + guidance | `ToolMessage(content="STEER_GUIDANCE: ...", status="error")` |

## Backtest Workflow

Before deploying to production, run an offline backtest against historical
execution traces:

1. Export 1,000-5,000 tool-call traces as JSONL (see `examples/backtest_sample_traces.jsonl`)
2. Run `python examples/run_backtest_demo.py` against your traces
3. Review the four-quadrant breakdown:
   - True Negative: correctly allowed benign operations
   - False Negative Caught: schema-valid calls that would have breached limits
   - False Positive Relieved: over-blocking that the simulator would have avoided
   - Steer Recovery Rate: percentage of blocked turns that could have completed via guidance

## Status

- **Financial simulator**: functional with unit tests
- **Database simulator**: functional with unit tests (EXPLAIN-based row estimation, recursive FK cascade walk)
- **Communications simulator**: functional with unit tests (blast radius, unsubscribe compliance, canary cohorts, reputation impact)
- **Strands integration**: functional with unit tests (full `ALLOW`/`DENY`/`ASK`/`STEER` lifecycle)
- **MCP integration**: functional with unit tests (stdio transport, JSON-RPC error handling)
- **LangGraph integration**: functional with unit tests (`@wrap_tool_call` middleware pattern)

## License

Apache-2.0
