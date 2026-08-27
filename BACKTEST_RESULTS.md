# Consequence-Gate Backtest Benchmark Results

**Date:** August 27, 2026  
**Artifact Version:** `v0.1.1`  
**Dataset:** `examples/benchmark_traces.jsonl` (500 agent tool-call traces)  
**Harness:** `consequence_gate.backtest.harness`

---

## Executive Summary

To evaluate `consequence-gate` against historical execution logs without requiring live production integrations, we evaluated a multi-domain synthetic trace corpus of 500 agent tool calls representing financial disbursements, database mutations, and communication blasts.

### Key Metrics

| Metric | Result | Description |
|---|---|---|
| **Total Traces Evaluated** | **500** | Full trace corpus spanning financial, database, and comms tools |
| **True Negatives** | **294** (58.8%) | Benign operations correctly identified and passed as `ALLOW` |
| **False Negatives Caught** | **87** (17.4%) | Schema-valid calls that breached velocity/blast limits and were intercepted (`DENY`/`STEER`/`ASK`) |
| **False Positives Relieved** | **59** (11.8%) | Benign operations over-blocked by static regex gates that `consequence-gate` safely enabled |
| **Other / Unclassified** | **60** (12.0%) | Ambiguous calls escalated for human review (`ASK`) |

---

## Trace Composition & Methodology

The benchmark trace dataset (`examples/benchmark_traces.jsonl`) contains 4 categories of tool-call patterns:

1. **Benign Agent Calls (60%):** Standard queries, micro-transfers with verified KYC, safe single-row updates, and scoped transactional notifications.
2. **Consequence Hazards (25%):** Calls that pass standard JSON schema validators and static regex checks, but cause severe downstream impact:
   - Financial transfers that exceed 24h rolling velocity limits (`INR 25,000` cap).
   - High-blast-radius unindexed database deletions (`150,000+` rows with cascading foreign keys).
   - Unsuppressed broadcast marketing campaigns without canary cohorts (`85,000+` recipients).
3. **Static Over-Blocking (10%):** Benign operations incorrectly blocked by static regex gates (e.g. `archive_old_session` blocked because it contains the keyword `delete`).
4. **Ambiguous Context (5%):** Unrecognized tools or missing session parameters that test whether the system safely falls back to `ASK`.

---

## How to Reproduce

You can reproduce this exact report locally using the CLI:

```bash
# 1. Ensure consequence-gate is installed
pip install -e .

# 2. Run the backtest against the benchmark dataset
consequence-gate backtest examples/benchmark_traces.jsonl

# 3. Or view machine-readable JSON output
consequence-gate backtest examples/benchmark_traces.jsonl --json
```

### CLI Output

```text
================ CONSEQUENCE GATE BACKTEST REPORT ================
Total Traces Evaluated:      500
True Negatives:              294
False Negatives Caught:      87
False Positives Relieved:    59
Other / Unclassified:        60
------------------------------------------------------------------
False Negative Catch Rate:   17.40%
False Positive Relief Rate:  11.80%
==================================================================
```
