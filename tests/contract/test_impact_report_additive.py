"""Check selection adds OPTIONAL impact-report fields; every existing 0.1.0 report must stay valid."""
import json
from pathlib import Path

import jsonschema
import pytest

from intentlock_policy.models import ImpactReport

ROOT = Path(__file__).resolve().parents[2] / "packages" / "contracts"
SCHEMA = json.loads((ROOT / "schemas" / "impact-report.schema.json").read_text())
FIXTURE = json.loads((ROOT / "fixtures" / "impact-report.json").read_text())


def test_legacy_report_without_new_fields_is_still_valid():
    assert "coverage" not in FIXTURE and "reason" not in FIXTURE["tests"][0]
    jsonschema.validate(FIXTURE, SCHEMA)
    assert ImpactReport.model_validate(FIXTURE).coverage is None


def test_new_fields_validate_in_schema_and_model():
    data = json.loads(json.dumps(FIXTURE))
    data["tests"][0]["reason"] = "matched src/auth/** (changed: src/auth/session.ts)"
    data["coverage"] = {"selected_checks": ["auth-tests"], "uncovered_paths": ["src/utils/x.ts"], "not_approved_checks": ["typecheck"]}
    jsonschema.validate(data, SCHEMA)
    model = ImpactReport.model_validate(data)
    assert model.coverage.uncovered_paths == ["src/utils/x.ts"]
    jsonschema.validate(json.loads(model.model_dump_json()), SCHEMA)


@pytest.mark.parametrize("bad", [{"selected_checks": [], "uncovered_paths": [], "extra": 1}, {"selected_checks": "auth-tests", "uncovered_paths": []}])
def test_malformed_coverage_is_rejected(bad):
    data = json.loads(json.dumps(FIXTURE)); data["coverage"] = bad
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, SCHEMA)
    with pytest.raises(Exception):
        ImpactReport.model_validate(data)
