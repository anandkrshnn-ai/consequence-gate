# Contributing to consequence-gate

Thank you for your interest in contributing to `consequence-gate`! This guide explains how to set up your development environment, run tests, and contribute new consequence simulators and integrations.

---

## Development Setup

### 1. Clone and Install

```bash
git clone https://github.com/anandkrshnn-ai/consequence-gate.git
cd consequence-gate
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e ".[dev]"
```

### 2. Run Test Suite & Coverage

```bash
pytest --cov=consequence_gate --cov-report=term-missing
```

All contributions are expected to maintain or improve test coverage.

### 3. Code Formatting & Linting

```bash
ruff check .
black --check .
```

---

## Core Architectural Invariants

When adding or modifying simulators, you must preserve these four core contracts:

1. **No Silent Argument Mutation:** Simulators must never alter input arguments in-flight. When an operation breaches safety boundaries, return a `STEER` decision containing structured alternative guidance that the agent or human must explicitly re-invoke.
2. **Deterministic Natural Keys:** Never generate random UUIDs for idempotency. Compute idempotency tokens by hashing the business entity's natural key (e.g. `user_id:amount:recipient_id`).
3. **Low Confidence Escalates to `ASK`:** If required session context is missing or cannot be accurately determined, set the simulation confidence below `0.8` to force human escalation (`ASK`). Never guess or default to `ALLOW`.
4. **Hard Retry Cap:** All integrations must route retries through `SteerCircuitBreaker` with a default cap of 2 retries before escalating to human review.

---

## Adding a New Domain Simulator

To contribute a new simulator (e.g., Cloud Infrastructure, File System, Code Execution):

1. Create a module under `consequence_gate/simulators/<domain>.py`.
2. Define a dataclass for the domain delta (e.g., `InfraDelta`).
3. Implement a simulator class with two core methods:
   - `simulate(tool_name, tool_args, session_context) -> DomainDelta`
   - `evaluate(delta, circuit_breaker) -> EvaluationResult`
4. Add comprehensive unit tests in `tests/test_<domain>_sim.py` covering:
   - Safe execution (`ALLOW`)
   - Limit breach triggering `STEER` with alternative suggestions
   - Critical policy violation (`DENY`)
   - Missing context / low confidence triggering `ASK`
   - Stable idempotency token generation across retries

---

## Pull Request Guidelines

1. **Branch Naming:** `feat/feature-name` or `fix/bug-description`.
2. **Commit Messages:** Follow [Conventional Commits](https://www.conventionalcommits.org/) (e.g. `feat: add infrastructure blast radius simulator`).
3. **Version Bumps:** Version bumps are handled during formal release cycles in synchronization with `__init__.py` and `pyproject.toml`.
