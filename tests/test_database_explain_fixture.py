"""Real-database validation of DataDeletionSimulator's PostgreSQL code paths.

These tests validate the simulator's accuracy claims against a live
PostgreSQL instance:

- ``get_planner_row_estimate`` returns planner row counts consistent with
  the actual data distribution (plain EXPLAIN -- the query is never
  executed).
- Index usage detection recognises Index Scan, Index Only Scan, and
  Bitmap Heap Scan plans (whose child nodes carry the Bitmap Index Scan).
- ``walk_fk_cascade_depth`` walks ON DELETE CASCADE foreign keys across
  multiple hops and excludes non-CASCADE references (NO ACTION).

Fixture schema (deterministic row counts)::

    accounts (1000 rows)
      +- claims (5000 rows, 5 per account, ON DELETE CASCADE)
           +- claim_line_items (20000 rows, 4 per claim, ON DELETE CASCADE)
    audit_log (100 rows, references accounts with ON DELETE NO ACTION)

Requirements: a reachable PostgreSQL and ``psycopg`` (a dev dependency).
In CI, GitHub Actions provides a postgres:16 service container. Locally
these tests are skipped when no PostgreSQL is reachable -- set
``CONSEQUENCE_GATE_TEST_PG_DSN`` to point at a local instance::

    CONSEQUENCE_GATE_TEST_PG_DSN="host=localhost dbname=postgres user=postgres" \
        pytest tests/test_database_explain_fixture.py -v
"""

import os

import pytest

from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.core.models import GateDecision
from consequence_gate.simulators.database import (
    DataDeletionSimulator,
    get_planner_row_estimate,
    walk_fk_cascade_depth,
)

PG_DSN = os.environ.get(
    "CONSEQUENCE_GATE_TEST_PG_DSN",
    "host=localhost port=5432 dbname=consequence_test user=consequence password=consequence",
)

N_ACCOUNTS = 1000
N_CLAIMS = 5000
N_LINE_ITEMS = 20000


@pytest.fixture(scope="module")
def pg():
    """A live PostgreSQL connection with a deterministic fixture schema.

    Skips the whole module when psycopg is not installed or no PostgreSQL
    is reachable, so local runs without a database still pass.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(PG_DSN, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip(
            "PostgreSQL not reachable; set CONSEQUENCE_GATE_TEST_PG_DSN to run "
            "the EXPLAIN fixture tests"
        )

    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS consequence_fixture CASCADE")
        cur.execute("CREATE SCHEMA consequence_fixture")
        # Session-level: all subsequent EXPLAIN / information_schema queries
        # in this module resolve tables through this schema.
        cur.execute("SET search_path TO consequence_fixture")

        cur.execute(
            """
            CREATE TABLE accounts (
                id SERIAL PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'active'
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE claims (
                id SERIAL PRIMARY KEY,
                account_id INTEGER NOT NULL
                    REFERENCES accounts(id) ON DELETE CASCADE,
                amount NUMERIC NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE claim_line_items (
                id SERIAL PRIMARY KEY,
                claim_id INTEGER NOT NULL
                    REFERENCES claims(id) ON DELETE CASCADE,
                quantity INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        # References accounts but NOT via CASCADE -- must never appear in a
        # cascade walk from accounts.
        cur.execute(
            """
            CREATE TABLE audit_log (
                id SERIAL PRIMARY KEY,
                account_id INTEGER REFERENCES accounts(id) ON DELETE NO ACTION,
                note TEXT
            )
            """
        )

        # Deterministic data: 1000 accounts, 5 claims each, 4 line items
        # per claim, half the claims 'open' and half 'closed'.
        cur.execute(
            "INSERT INTO accounts (status) SELECT 'active' FROM generate_series(1, %s)",
            [N_ACCOUNTS],
        )
        cur.execute(
            """
            INSERT INTO claims (account_id, amount, status)
            SELECT (i %% 1000) + 1, 100.0,
                   CASE WHEN i %% 2 = 0 THEN 'open' ELSE 'closed' END
            FROM generate_series(1, %s) AS i
            """,
            [N_CLAIMS],
        )
        cur.execute(
            """
            INSERT INTO claim_line_items (claim_id, quantity)
            SELECT (i %% 5000) + 1, 1
            FROM generate_series(1, %s) AS i
            """,
            [N_LINE_ITEMS],
        )
        # No params on this one, so a single % (no escaping needed).
        cur.execute(
            """
            INSERT INTO audit_log (account_id, note)
            SELECT (i % 1000) + 1, 'note'
            FROM generate_series(1, 100) AS i
            """
        )
        cur.execute("CREATE INDEX idx_claims_account_id ON claims(account_id)")

        # Real statistics for the planner -- without ANALYZE the estimates
        # fall back to defaults and the assertions below would be vacuous.
        cur.execute("ANALYZE")
    conn.commit()

    yield conn

    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS consequence_fixture CASCADE")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# get_planner_row_estimate: row-count accuracy from the real planner
# ---------------------------------------------------------------------------


class TestPlannerRowEstimate:
    def test_full_table_estimate_is_accurate(self, pg):
        """No filters: the planner should report the full table size
        (1000 rows for a freshly ANALYZEd table)."""
        rows, used_index = get_planner_row_estimate(pg, "accounts", {})
        assert abs(rows - N_ACCOUNTS) <= N_ACCOUNTS * 0.10
        assert used_index is False  # unfiltered scan is a Seq Scan

    def test_indexed_filter_estimate_is_accurate(self, pg):
        """Filtering claims by an indexed account_id (5 claims per account
        out of 5000) should produce a small, accurate estimate and be
        recognised as index-backed (Index Scan or Bitmap Heap Scan)."""
        rows, used_index = get_planner_row_estimate(pg, "claims", {"account_id": 5})
        assert 1 <= rows <= 50
        assert used_index is True

    def test_unindexed_filter_estimate_is_accurate(self, pg):
        """Filtering claims by an unindexed status column (half the rows
        are 'open') should estimate ~2500 rows via a sequential scan."""
        rows, used_index = get_planner_row_estimate(pg, "claims", {"status": "open"})
        assert 1000 <= rows <= 4000
        assert used_index is False

    def test_explain_never_executes_the_query(self, pg):
        """get_planner_row_estimate must use plain EXPLAIN, not EXPLAIN
        ANALYZE. The planner reports a minimum of 1 row for a scan node,
        so an empty table estimates 1, not 0."""
        with pg.cursor() as cur:
            cur.execute("CREATE TABLE empty_probe (id INTEGER PRIMARY KEY, v TEXT)")
            cur.execute("ANALYZE empty_probe")
        pg.commit()
        try:
            rows, _ = get_planner_row_estimate(pg, "empty_probe", {})
            assert rows <= 1
        finally:
            with pg.cursor() as cur:
                cur.execute("DROP TABLE empty_probe")
            pg.commit()


# ---------------------------------------------------------------------------
# walk_fk_cascade_depth: cascade graph accuracy
# ---------------------------------------------------------------------------


class TestFkCascadeWalk:
    def test_accounts_cascade_reaches_two_hops(self, pg):
        """accounts -> claims -> claim_line_items (two CASCADE hops)."""
        affected = walk_fk_cascade_depth(pg, "accounts")
        assert set(affected) == {"accounts", "claims", "claim_line_items"}
        assert affected[0] == "accounts"  # root table first

    def test_claims_cascade_reaches_one_hop(self, pg):
        affected = walk_fk_cascade_depth(pg, "claims")
        assert set(affected) == {"claims", "claim_line_items"}

    def test_leaf_table_has_no_cascades(self, pg):
        affected = walk_fk_cascade_depth(pg, "claim_line_items")
        assert affected == ["claim_line_items"]

    def test_no_action_reference_not_included(self, pg):
        """audit_log references accounts, but with ON DELETE NO ACTION --
        it must not appear in the cascade walk from accounts."""
        affected = walk_fk_cascade_depth(pg, "accounts")
        assert "audit_log" not in affected

    def test_audit_log_itself_is_a_leaf(self, pg):
        affected = walk_fk_cascade_depth(pg, "audit_log")
        assert affected == ["audit_log"]


# ---------------------------------------------------------------------------
# DataDeletionSimulator end-to-end against a live planner
# ---------------------------------------------------------------------------


class TestDataDeletionSimulatorWithLivePg:
    def test_simulator_uses_planner_and_cascade_graph(self, pg):
        """simulate() with a live db_conn must report the planner's row
        estimate and the real cascade graph, with high confidence. Filters
        are required for a natural key (unfiltered deletes escalate to ASK
        by design)."""
        sim = DataDeletionSimulator(max_autonomous_delete_rows=100, db_conn=pg)
        delta = sim.simulate(
            "bulk_delete",
            {"table": "claims", "filters": {"status": "open"}, "hard_delete": True},
            {},
        )

        assert delta.confidence == pytest.approx(0.90)
        # 'open' claims are half of 5000
        assert 1000 <= delta.estimated_affected_rows <= 4000
        assert delta.has_active_foreign_key_cascades is True
        assert "claim_line_items" in delta.cascade_affected_tables
        assert "audit_log" not in delta.cascade_affected_tables

    def test_unfiltered_delete_escalates_by_design(self, pg):
        """A delete with no filters has no natural key (no stable
        idempotency identity) and must escalate to ASK even with a live
        planner connection."""
        sim = DataDeletionSimulator(max_autonomous_delete_rows=100, db_conn=pg)
        delta = sim.simulate(
            "purge_all", {"table": "claims", "filters": {}, "hard_delete": True}, {}
        )
        result = sim.evaluate(delta, SteerCircuitBreaker())
        assert result.decision == GateDecision.ASK

    def test_large_hard_delete_is_denied(self, pg):
        """A hard delete of ~2500 'open' claims is 25x the autonomous
        threshold (100) and beyond the 10x hard-delete ceiling -> DENY."""
        sim = DataDeletionSimulator(max_autonomous_delete_rows=100, db_conn=pg)
        delta = sim.simulate(
            "purge", {"table": "claims", "filters": {"status": "open"}, "hard_delete": True}, {}
        )
        result = sim.evaluate(delta, SteerCircuitBreaker())
        assert result.decision == GateDecision.DENY

    def test_bounded_indexed_delete_is_allowed(self, pg):
        """A ~5-row soft delete, planner-verified, is within the envelope."""
        sim = DataDeletionSimulator(max_autonomous_delete_rows=100, db_conn=pg)
        delta = sim.simulate(
            "cleanup",
            {"table": "claims", "filters": {"account_id": 5}, "hard_delete": False},
            {},
        )
        result = sim.evaluate(delta, SteerCircuitBreaker())
        assert result.decision == GateDecision.ALLOW

    def test_mid_size_delete_is_steered(self, pg):
        """A 2500-row (unindexed 'open' status) soft delete exceeds the
        100-row threshold -> STEER with guidance."""
        sim = DataDeletionSimulator(max_autonomous_delete_rows=100, db_conn=pg)
        delta = sim.simulate(
            "archive",
            {"table": "claims", "filters": {"status": "open"}, "hard_delete": False},
            {},
        )
        assert delta.estimated_affected_rows > 100
        result = sim.evaluate(delta, SteerCircuitBreaker())
        assert result.decision == GateDecision.STEER
        assert result.steer_payload is not None
