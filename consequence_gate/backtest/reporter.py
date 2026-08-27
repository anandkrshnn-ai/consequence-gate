"""
Generates the four-quadrant FP/FN breakdown report from backtest results.
"""

from collections import Counter


def generate_report(results: list[dict]) -> dict:
    counts = Counter(r["quadrant"] for r in results)
    total = len(results)
    fn_caught = counts.get("FALSE_NEGATIVE_CAUGHT", 0)
    fp_relieved = counts.get("FALSE_POSITIVE_RELIEVED", 0)
    return {
        "total_traces": total,
        "true_negative": counts.get("TRUE_NEGATIVE", 0),
        "hazards_intercepted": fn_caught,
        "overblocked_relieved": fp_relieved,
        "other": counts.get("OTHER", 0),
        "hazard_interception_rate": fn_caught / total if total else 0.0,
        "overblock_relief_rate": fp_relieved / total if total else 0.0,
        # Backward-compatibility aliases
        "false_negative_caught": fn_caught,
        "false_positive_relieved": fp_relieved,
        "false_negative_rate": fn_caught / total if total else 0.0,
        "false_positive_relief_rate": fp_relieved / total if total else 0.0,
    }
