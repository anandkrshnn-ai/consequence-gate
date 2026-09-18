#!/usr/bin/env python3
import json
import random


def main():
    input_file = "traces_from_agent_crash_test.jsonl"
    output_sample = "sample_ambiguous_success.jsonl"
    sample_size = 20

    candidates = []
    with open(input_file, encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            # 1. ground_truth_consequence == "AMBIGUOUS"
            # 2. actual_outcome == "success" (tool call succeeded)
            # 3. metadata.success == True (agent reported success)
            # 4. chaos_type is not "none" (something happened)

            # Using trajectory_success because of my rewrite
            if (
                entry.get("ground_truth_consequence") == "AMBIGUOUS"
                and entry.get("actual_outcome") == "success"
                and entry.get("step_context", {}).get("trajectory_success") is True
                and entry.get("chaos_type") != "none"
            ):
                candidates.append(entry)

    if len(candidates) < sample_size:
        sample_size = len(candidates)
        print(f"Only {len(candidates)} candidates found. Sampling all.")

    sample = random.sample(candidates, sample_size) if sample_size > 0 else []

    with open(output_sample, "w", encoding="utf-8") as out:
        for entry in sample:
            out.write(json.dumps(entry) + "\n")

    print(f"Wrote {len(sample)} entries to {output_sample}")
    print("Trace IDs for manual review:")
    for entry in sample:
        print(f"  - {entry['trace_id']} (chaos: {entry['chaos_type']})")


if __name__ == "__main__":
    main()
