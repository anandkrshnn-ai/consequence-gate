"""
Generates the four-quadrant FP/FN breakdown report from backtest results.
"""

from collections import Counter
from typing import Dict, List


def generate_report(results: List[Dict]) -> Dict:
    counts = Counter(r["quadrant"] for r in results)
    total = len(results)
    return {
        "total_traces": total,
        "true_negative": counts.get("TRUE_NEGATIVE", 0),
        "false_negative_caught": counts.get("FALSE_NEGATIVE_CAUGHT", 0),
        "false_positive_relieved": counts.get("FALSE_POSITIVE_RELIEVED", 0),
        "other": counts.get("OTHER", 0),
        "false_negative_rate": counts.get("FALSE_NEGATIVE_CAUGHT", 0) / total if total else 0.0,
        "false_positive_relief_rate": counts.get("FALSE_POSITIVE_RELIEVED", 0) / total if total else 0.0,
    }
