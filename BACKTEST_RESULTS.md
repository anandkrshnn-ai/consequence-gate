# Consequence-Gate Backtest Benchmark Results

> [!IMPORTANT]
> **Synthetic Dataset & Demo Evaluator Disclosure:**  
> 1. **Synthetic Data:** The traces evaluated in this report were **synthetically generated** (`examples/benchmark_traces.jsonl`) to validate the offline backtest harness, simulator logic, and four-quadrant classification pipeline end-to-end. They do **not** represent production traffic or real-world customer telemetry.
> 2. **Evaluator Scope:** The out-of-the-box CLI runner uses a standalone heuristic evaluator (`cli.demo_evaluator`) calibrated specifically to demonstrate the harness mechanics without requiring live PostgreSQL instances or payment APIs. In production, users pass their configured domain simulators (`FinancialDeltaPredictor`, `DatabaseConsequencePredictor`, `CommunicationBlastPredictor`) into `run_backtest(traces, evaluator)`.
> 3. **100% Recall Context:** The 100% recall on this synthetic corpus reflects exact matching against the known generation constraints of this sample dataset, proving harness pipeline correctness rather than generalization across unconstrained enterprise workloads.

**Date:** August 27, 2026  
**Artifact Version:** `v0.1.1`  
**Dataset:** `examples/benchmark_traces.jsonl` (500 synthetically generated agent tool-call traces)  
**Harness:** `consequence_gate.backtest.harness`

---

## Executive Summary

To validate `consequence-gate`'s evaluation pipeline without requiring live database or payment provider connections, we evaluated a multi-domain synthetic trace corpus of 500 agent tool calls (financial disbursements, database mutations, and communication blasts).

### Key Metrics (500 Synthetic Traces)

| Metric | Result | Description |
|---|---|---|
| **Total Traces Evaluated** | **500** | Full synthetic corpus spanning financial, database, and comms tools |
| **Benign Pass-Through (True Negatives)** | **252** (50.4%) | Benign operations correctly identified and passed as `ALLOW` |
| **Downstream Hazards Intercepted** | **129** (25.8%) | Schema-valid calls that would breach velocity/blast limits, intercepted by the gate (`DENY`/`STEER`/`ASK`) — *100% recall (129/129) on this synthetic corpus* |
| **Over-Blocked Operations Relieved** | **35** (7.0%) | Benign operations over-blocked by naive regex gates that `consequence-gate` safely permitted (35 of 52 relieved; remaining 17 conservatively flagged for human review) |
| **Ambiguous / Escalated to Human** | **84** (16.8%) | Intentional ambiguous test cases (24 traces) + safe high-value/deletion operations conservatively escalated by the heuristic (60 traces) |

---

## Trace Composition & Methodology

The benchmark trace dataset (`examples/benchmark_traces.jsonl`) contains 4 categories of tool-call patterns:

1. **Benign Agent Calls (295 traces / 59.0%):** Standard queries, micro-transfers with verified KYC, safe single-row updates, and scoped transactional notifications.
2. **Consequence Hazards (129 traces / 25.8%):** Calls that pass standard JSON schema validators and static regex checks, but cause severe downstream impact:
   - Financial transfers that exceed 24h rolling velocity limits (`INR 25,000` cap) — **32 traces**.
   - High-blast-radius unindexed database deletions (`150,000+` rows with cascading foreign keys) — **55 traces**.
   - Unsuppressed broadcast marketing campaigns without canary cohorts (`85,000+` recipients) — **42 traces**.
3. **Static Over-Blocking (52 traces / 10.4%):** Benign operations incorrectly blocked by static regex gates (e.g. `archive_old_session` blocked because it contains the keyword `delete`).
4. **Ambiguous Context (24 traces / 4.8%):** Unrecognized tools or missing session parameters that test whether the system safely falls back to `ASK`.

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
[!] NOTICE: Running with built-in demo_evaluator (synthetic trace heuristic).
    For production evaluation, pass domain simulator instances. See BACKTEST_RESULTS.md.

================ CONSEQUENCE GATE BACKTEST REPORT ================
Total Traces Evaluated:      500
Benign Pass-Through (TN):    252
Hazards Intercepted:         129
Over-Blocked Relieved (FP):  35
Ambiguous / Escalated:       84
------------------------------------------------------------------
Hazard Interception Share:   25.80%
False Positive Relief Share: 7.00%
==================================================================
```
