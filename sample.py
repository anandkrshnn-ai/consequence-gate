import json
import random

with open("traces_from_agent_crash_test.jsonl", encoding="utf-8") as f:
    ambiguous = [json.loads(line) for line in f if "AMBIGUOUS" in line]
sample = random.sample(ambiguous, min(5, len(ambiguous)))
for s in sample:
    print(f"Trace: {s['trace_id']}, Chaos: {s['chaos_type']}, Tool: {s['tool_name']}")
