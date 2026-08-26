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

- `consequence_gate.simulators.financial` -- disbursement / claim / refund
  velocity and irreversibility modeling.
- `consequence_gate.simulators.database` -- row-count blast radius via the
  DB's own query planner (`EXPLAIN`, not hardcoded selectivity constants)
  and recursive `ON DELETE CASCADE` graph walking.
- `consequence_gate.core` -- shared models, the confidence/threshold
  evaluator, and the idempotency-locked circuit breaker.
- `consequence_gate.integrations` -- stubs for MCP, Strands, and
  LangGraph wiring (not yet implemented -- contributions welcome).
- `consequence_gate.backtest` -- offline JSONL trace replay harness and
  four-quadrant FP/FN/TN report generator, for evaluating this layer
  against historical execution logs with zero production integration.

## Quickstart

```bash
pip install -e ".[dev]"
pytest
python examples/run_backtest_demo.py
```

## Status

Early-stage. Financial and database simulators are functional with unit
tests. Framework integrations (MCP, Strands, LangGraph) are stubs pending
real wiring. Communications-domain simulator not yet started.

## License

Apache-2.0
