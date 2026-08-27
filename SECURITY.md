# Security Policy

## Reporting Security Vulnerabilities

If you discover a security vulnerability or bypass vector in `consequence-gate`, please **do not** open a public GitHub issue.

Please report vulnerabilities privately:
- **Email:** [anandkrshnn@gmail.com](mailto:anandkrshnn@gmail.com)
- **Subject Line:** `[SECURITY] consequence-gate vulnerability report`
- **Response Timeline:** You will receive an initial response within 24 hours acknowledging receipt and a remediation plan within 72 hours.

---

## Security Model & Trust Boundaries

`consequence-gate` is designed as a speculative consequence-prediction layer that runs before irreversible tools are invoked by AI agents. Understanding the trust boundaries is critical for secure deployment:

### 1. The Context Provider Dependency
- **Boundary:** `consequence-gate` relies on `context_provider` callbacks supplied by your application to provide state (e.g. `account_rolling_24h_spend`, `kyc_verified`, `table_metadata`, `segment_counts`).
- **Integrator Responsibility:** The context provider MUST fetch fresh, authoritative state directly from your production datastore or authenticated session context.
- **Fail-Safe Contract:** If the context provider returns missing or uncertain values, `consequence-gate` drops its internal confidence metric below the threshold (default: 0.8) and **escalates to `ASK` (human review)**. It never defaults to a speculative `ALLOW` when context is absent.

### 2. Upstream Positioning (Defense-in-Depth)
- `consequence-gate` is designed to run **in front of** existing static policy engines (e.g. AgentWall, IAM roles, DB RBAC, Strands `BeforeToolCallEvent` filters).
- It should **never** be used as a replacement for database-level permissions (e.g. DB read-only users, row-level security) or cryptographic authorization tokens.

### 3. Non-Mutating Steering
- When an operation exceeds safety thresholds, `consequence-gate` issues a `STEER` response containing structured suggestions for safer parameters.
- `consequence-gate` **never silently mutates tool arguments**. The agent or human operator must explicitly issue the revised call to preserve audit trail integrity.

### 4. Natural-Key Idempotency
- Idempotency tokens are cryptographically hashed from the transaction's natural key (e.g. `user_id:amount:recipient` or `table:filter_hash`).
- This prevents replay attacks and ensures that network retry loops cannot duplicate transactions or bypass circuit breakers.

---

## Supported Versions

| Version | Supported |
|---|---|
| 0.1.x | :white_check_mark: |
| < 0.1.0 | :x: |
