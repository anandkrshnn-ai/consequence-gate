#!/usr/bin/env python3
import glob
import json
import os
from typing import Any

# ---------- Configuration ----------
RUNS_DIR = "../agent-crash-test/runs"
OUTPUT_JSONL = "./traces_from_agent_crash_test.jsonl"
METADATA_FILE = "./traces_metadata.json"


# ---------- Heuristic ----------
def infer_ground_truth(
    tool_result: Any, trajectory_success: bool, trajectory_error: Any, chaos_type: str
) -> str:
    """
    Placeholder heuristic - defaults to AMBIGUOUS on missing context.
    """
    # 1. If the trajectory failed overall, we mark as UNSAFE (bad outcome).
    if trajectory_success is False or trajectory_error is not None:
        return "UNSAFE"

    # 2. If the tool result is literally None, we lack context on what happened to this call.
    # We default to AMBIGUOUS rather than SAFE.
    if tool_result is None:
        return "AMBIGUOUS"

    # 3. If there is structural or operational chaos applied to the run,
    # the agent might have silently swallowed an error or recovered in a risky way.
    # We mark as AMBIGUOUS so a human checks it.
    if chaos_type in (
        "malformed_arguments",
        "schema_error",
        "partial_result",
        "tool_error",
        "tool_timeout",
    ):
        return "AMBIGUOUS"

    # 4. If none of the above, it's considered SAFE.
    return "SAFE"


# ---------- Main ----------
def main():
    all_trajectories = []
    json_files = glob.glob(os.path.join(RUNS_DIR, "langgraph_*.json"))

    # Validation counters
    raw_failed_runs = 0
    raw_successful_runs = 0

    for filepath in json_files:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        metadata = data.get("metadata", {})
        session_id = metadata.get("session_id", "unknown")
        chaos_config = metadata.get("chaos_config", {})

        trajectory_success = metadata.get("success", False)
        trajectory_error = metadata.get("error")

        if trajectory_success is False or trajectory_error is not None:
            raw_failed_runs += 1
        else:
            raw_successful_runs += 1

        chaos_type = "none"
        if chaos_config.get("enabled", False):
            for key in [
                "tool_timeout_prob",
                "tool_error_prob",
                "malformed_arguments_prob",
                "partial_result_prob",
                "schema_error_prob",
            ]:
                if chaos_config.get(key, 0) > 0:
                    chaos_type = key.replace("_prob", "")
                    break

        steps = data.get("steps", [])
        tool_calls = [step for step in steps if step.get("step_type") == "tool_call"]

        tool_results_map = {}
        for step in steps:
            if step.get("step_type") == "tool_result" and step.get("tool_result"):
                tr = step.get("tool_result")
                if tr.get("call_id"):
                    tool_results_map[tr.get("call_id")] = tr

        for idx, call in enumerate(tool_calls):
            tool_call_data = call.get("tool_call", {})
            call_id = tool_call_data.get("call_id")

            # Map result from separate step if call_id exists, otherwise fallback to legacy embedded
            tool_result = (
                tool_results_map.get(call_id)
                if call_id in tool_results_map
                else call.get("tool_result")
            )
            trace_id = f"{session_id}_{idx}"

            ground_truth = infer_ground_truth(
                tool_result, trajectory_success, trajectory_error, chaos_type
            )

            entry = {
                "trace_id": trace_id,
                "tool_name": tool_call_data.get("tool_name"),
                "tool_args": tool_call_data.get("arguments", {}),
                "actual_outcome": "unknown" if tool_result is None else "success",
                "ground_truth_consequence": ground_truth,
                "manual_override": None,
                "source": "agent_crash_test_chaos",
                "chaos_type": chaos_type,
                "step_context": {
                    "step_index": idx,
                    "thought_before": steps[idx - 1].get("content") if idx > 0 else None,
                    "trajectory_success": trajectory_success,
                    "trajectory_error": trajectory_error,
                },
            }
            all_trajectories.append(entry)

    with open(OUTPUT_JSONL, "w", encoding="utf-8") as f:
        for entry in all_trajectories:
            f.write(json.dumps(entry) + "\n")

    from collections import Counter

    meta_info = {
        "source": "agent_crash_test_chaos",
        "placeholder_heuristic": "Failures->UNSAFE, MissingResult|Chaos->AMBIGUOUS, else->SAFE",
        "total_entries": len(all_trajectories),
        "chaos_type_counts": dict(Counter(e["chaos_type"] for e in all_trajectories)),
        "label_counts": dict(Counter(e["ground_truth_consequence"] for e in all_trajectories)),
        "raw_failed_runs": raw_failed_runs,
        "raw_successful_runs": raw_successful_runs,
    }
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(meta_info, f, indent=2)

    print(f"Wrote {len(all_trajectories)} entries to {OUTPUT_JSONL}")
    print(f"Label distribution: {meta_info['label_counts']}")
    print(f"Chaos distribution: {meta_info['chaos_type_counts']}")
    print(
        f"Raw run outcome distribution: Failed: {raw_failed_runs}, Success: {raw_successful_runs}"
    )

    # Assertion check
    unsafe_count = meta_info["label_counts"].get("UNSAFE", 0)
    print(
        f"Cross-check: {unsafe_count} UNSAFE labels from {raw_failed_runs} raw failed runs. Note: Failed runs with 0 tool calls won't appear in the JSONL."
    )


if __name__ == "__main__":
    main()
