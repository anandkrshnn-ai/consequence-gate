#!/usr/bin/env python3
"""Offline verifier for ConsequenceEvidenceEnvelope JSON.

Does not import domain simulators or integration hooks.
"""

from __future__ import annotations

import argparse
import json
import sys

from consequence_gate.core.evidence import ConsequenceEvidenceEnvelope, verify_envelope


def verify_dict(payload: dict) -> bool:
    envelope = ConsequenceEvidenceEnvelope.model_validate(payload)
    return verify_envelope(envelope)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a signed consequence evidence envelope")
    parser.add_argument("envelope", help="Path to envelope JSON")
    args = parser.parse_args(argv)
    with open(args.envelope, encoding="utf-8") as handle:
        payload = json.load(handle)
    ok = verify_dict(payload)
    print("VALID" if ok else "INVALID")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
