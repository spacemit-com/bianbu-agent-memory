from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "external_api_contract_demo.py"
SPEC = importlib.util.spec_from_file_location("external_api_contract_demo", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
DEMO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEMO)


def test_openai_mock_exercises_the_production_planner_contract() -> None:
    result = asyncio.run(DEMO.exercise_model_api())
    assert result["status"] == "PASS"
    assert result["evidence_level"] == "mock_contract"
    assert result["requests"] == 2
    assert result["authorization_header_verified"] is True
    assert result["response_format_verified"] is True
    assert result["mutation"] == "ADD"


def test_external_gaps_are_never_reported_as_passed() -> None:
    gaps = DEMO.external_resource_gaps(live_model=False, runtime_checked=False)
    assert gaps
    assert all(item["status"] == "SKIP_EXTERNAL_RESOURCE" for item in gaps)
    covered = {spec for item in gaps for spec in item["specs"]}
    assert {"MEM-INT-02", "MEM-INT-04", "MEM-INT-08", "MEM-L4-06"} <= covered
