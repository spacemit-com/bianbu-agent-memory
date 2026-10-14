#!/usr/bin/env python3
"""Deterministic acceptance gate for L2 summaries.

The gate is intentionally model-agnostic: it checks the facts required by the
Memory contract, operation coverage, and the zero-secret invariant.  It can be
used against a daemon response or a captured JSON summary in CI/K3 tests.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SECRET = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|bearer)\s*(?:[:=]|\s+[A-Za-z0-9_\-]{12,})")
FIELDS = (
    "current_status",
    "key_decisions",
    "open_questions",
    "next_steps",
    "files_resources_touched",
    "commands_executed",
    "operations_completed",
    "errors_encountered",
)


def validate(summary: dict, expected: list[str]) -> list[str]:
    errors: list[str] = []
    for field in FIELDS:
        value = summary.get(field, "" if field == "current_status" else [])
        if field == "current_status":
            if not isinstance(value, str) or len(value) > 8192:
                errors.append("current_status must be a string <= 8192 bytes")
        elif not isinstance(value, list) or len(value) > 100 or not all(
            isinstance(item, str) and len(item) <= 8192 for item in value
        ):
            errors.append(f"{field} must be at most 100 strings <= 8192 bytes")
    text = json.dumps(summary, ensure_ascii=False)
    if SECRET.search(text):
        errors.append("summary contains a credential-like field")
    lowered = text.casefold()
    for fact in expected:
        if fact.casefold() not in lowered:
            errors.append(f"missing expected fact: {fact}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", type=Path, help="JSON file containing the summary object")
    parser.add_argument("--expect", action="append", default=[], help="fact that must be recalled")
    args = parser.parse_args()
    value = json.loads(args.summary.read_text(encoding="utf-8-sig"))
    summary = value.get("summary", value.get("summary_payload", value))
    if not isinstance(summary, dict):
        print("summary is not an object", file=sys.stderr)
        return 2
    errors = validate(summary, args.expect)
    if errors:
        print("FAIL")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("PASS: L2 schema, operation coverage and secret-safety checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
