"""
DataDeletionSimulator: outcome simulator for destructive DB operations.

Row-count estimation uses the database's own query planner (EXPLAIN, not
EXPLAIN ANALYZE -- so the query is never actually executed) rather than
hardcoded selectivity constants, which have no statistical grounding and
are demonstrably less accurate than even a real planner's known-imperfect
estimates. FK cascade detection walks the graph depth-first and
recursively, since ON DELETE CASCADE can compound across multiple hops.
"""

from dataclasses import dataclass, field
from typing import Any

from ..core.circuit_breaker import SteerCircuitBreaker
from ..core.models import EvaluationResult, GateDecision


@dataclass
class DeletionBlastDelta:
    tool_name: str
    target_table: str
    estimated_affected_rows: int
    has_unindexed_where_clause: bool
    is_hard_delete: bool
    has_active_foreign_key_cascades: bool
    cascade_affected_tables: list[str]
    irreversibility_score: float
    confidence: float
    natural_key: str
    simulated_side_effects: list[str] = field(default_factory=list)


def get_planner_row_estimate(db_conn, table: str, filters: dict[str, Any]) -> tuple:
    """Returns (estimated_rows, used_index). Uses plain EXPLAIN so the
    query is analyzed but never executed."""
    if not filters:
        where_clause = "TRUE"
        params: list[Any] = []
    else:
        where_clause = " AND ".join(f"{k} = %s" for k in filters)
        params = list(filters.values())

    query = f"EXPLAIN (FORMAT JSON) SELECT 1 FROM {table} WHERE {where_clause}"
    plan = db_conn.execute(query, params).fetchone()[0]
    root = plan[0]["Plan"]
    estimated_rows = root.get("Plan Rows", 0)
    used_index = "Index" in root.get("Node Type", "")
    return estimated_rows, used_index


def walk_fk_cascade_depth(db_conn, table: str, visited: set[str] | None = None) -> list[str]:
    """Recursively walks ON DELETE CASCADE foreign keys depth-first."""
    if visited is None:
        visited = set()
    if table in visited:
        return []
    visited.add(table)

    rows = db_conn.execute(
        """
        SELECT tc.table_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.referential_constraints rc
          ON tc.constraint_name = rc.constraint_name
        JOIN information_schema.constraint_column_usage ccu
          ON rc.unique_constraint_name = ccu.constraint_name
        WHERE ccu.table_name = %s AND rc.delete_rule = 'CASCADE'
        """,
        [table],
    ).fetchall()

    affected = [table]
    for (child_table,) in rows:
        affected.extend(walk_fk_cascade_depth(db_conn, child_table, visited))
    return affected


class DataDeletionSimulator:
    def __init__(self, max_autonomous_delete_rows: int = 100, db_conn=None):
        self.max_autonomous_delete_rows = max_autonomous_delete_rows
        self.db_conn = db_conn  # optional live connection for EXPLAIN / FK introspection

    def simulate(
        self, tool_name: str, args: dict[str, Any], context: dict[str, Any]
    ) -> DeletionBlastDelta:
        table = args.get("table", "unknown")
        filters = args.get("filters", {})
        force_hard_delete = args.get("hard_delete", False)
        natural_key = f"{table}:{sorted(filters.items())}"

        table_stats = context.get("table_metadata", {}).get(table, {})
        total_table_rows = table_stats.get("total_rows", 0)

        if self.db_conn is not None:
            estimated_rows, used_index = get_planner_row_estimate(self.db_conn, table, filters)
            unindexed = not used_index
            cascade_tables = walk_fk_cascade_depth(self.db_conn, table)[1:]
        else:
            estimated_rows = total_table_rows
            unindexed = True
            cascade_tables = table_stats.get("cascade_children", [])

        has_cascades = len(cascade_tables) > 0
        irreversibility = 1.0 if force_hard_delete else 0.2
        confidence = 0.90 if self.db_conn is not None else 0.40

        side_effects = []
        if estimated_rows > self.max_autonomous_delete_rows:
            side_effects.append(
                f"Estimated row deletion ({estimated_rows}) exceeds safe autonomous threshold "
                f"({self.max_autonomous_delete_rows})"
            )
        if has_cascades:
            side_effects.append(
                f"FK cascade will propagate to {len(cascade_tables)} additional table(s): {cascade_tables}"
            )
        if unindexed:
            side_effects.append("Full table scan required; risk of lock escalation")

        return DeletionBlastDelta(
            tool_name=tool_name,
            target_table=table,
            estimated_affected_rows=estimated_rows,
            has_unindexed_where_clause=unindexed,
            is_hard_delete=force_hard_delete,
            has_active_foreign_key_cascades=has_cascades,
            cascade_affected_tables=cascade_tables,
            irreversibility_score=irreversibility,
            confidence=confidence,
            natural_key=natural_key,
            simulated_side_effects=side_effects,
        )

    def evaluate(
        self, delta: DeletionBlastDelta, circuit_breaker: SteerCircuitBreaker
    ) -> EvaluationResult:
        if delta.confidence < 0.70:
            return EvaluationResult(
                decision=GateDecision.ASK,
                confidence=delta.confidence,
                reason="Missing live query-planner access; unable to calculate blast radius with confidence.",
            )

        if (
            delta.estimated_affected_rows > (self.max_autonomous_delete_rows * 10)
            and delta.is_hard_delete
        ):
            return EvaluationResult(
                decision=GateDecision.DENY,
                confidence=delta.confidence,
                reason=f"CRITICAL BLAST RADIUS: hard delete would purge ~{delta.estimated_affected_rows:,} rows.",
            )

        if delta.estimated_affected_rows > self.max_autonomous_delete_rows or delta.is_hard_delete:
            base_steer = {
                "guidance": (
                    f"Direct hard deletion of {delta.estimated_affected_rows} rows from `{delta.target_table}` "
                    f"is restricted. Option A: `archive_and_soft_delete` (sets deleted_at, 30-day retention). "
                    f"Option B: if permanent deletion is required, call `request_bulk_purge_authorization`. "
                    f"Note: soft delete requires all downstream queries to filter deleted_at IS NULL."
                ),
                "suggested_tool": "archive_and_soft_delete",
                "suggested_args": {
                    "table": delta.target_table,
                    "filters": {"retention_status": "expired"},
                    "mode": "soft_delete",
                },
            }
            return circuit_breaker.resolve(delta.natural_key, delta.confidence, base_steer)

        return EvaluationResult(
            decision=GateDecision.ALLOW,
            confidence=delta.confidence,
            reason=f"Delete operation is bounded (~{delta.estimated_affected_rows} rows) within safety envelope.",
        )
