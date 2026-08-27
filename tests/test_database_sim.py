from consequence_gate.core.circuit_breaker import SteerCircuitBreaker
from consequence_gate.core.models import GateDecision
from consequence_gate.simulators.database import DataDeletionSimulator


def test_no_db_conn_low_confidence_asks():
    sim = DataDeletionSimulator(max_autonomous_delete_rows=100, db_conn=None)
    breaker = SteerCircuitBreaker()
    args = {"table": "orders", "filters": {}, "hard_delete": True}
    context = {"table_metadata": {"orders": {"total_rows": 500000}}}
    delta = sim.simulate("delete_records", args, context)
    result = sim.evaluate(delta, breaker)
    assert result.decision == GateDecision.ASK


def test_no_hardcoded_selectivity_fallback_is_conservative():
    sim = DataDeletionSimulator(max_autonomous_delete_rows=100, db_conn=None)
    args = {"table": "orders", "filters": {"id": 42}, "hard_delete": False}
    context = {"table_metadata": {"orders": {"total_rows": 500000}}}
    delta = sim.simulate("delete_records", args, context)
    assert delta.estimated_affected_rows == 500000
