"""Shared-schema 0.1.0 contract tests: JSON Schema <-> Pydantic <-> fixtures."""
import json
import pathlib

import jsonschema
import pytest
from pydantic import ValidationError

from intentlock_policy.models import SCHEMA_VERSION, SHARED_MODELS

ROOT = pathlib.Path(__file__).resolve().parents[2] / "packages" / "contracts"
FIXTURES = {
    "mission-contract": ["contract.json"],
    "mission-contract-draft": ["contract-draft.json"],
    "tool-request": ["tool-request.read-allowed.json", "tool-request.read-env.json"],
    "authorization-decision": ["authorization-decision.allow.json", "authorization-decision.deny.json"],
    "run": ["run.json"],
    "event": ["event.json"],
    "impact-report": ["impact-report.json"],
    "review-approval": ["review-approval.json"],
}


def schema(name):
    return json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())


def cases():
    return [(n, f) for n, fs in FIXTURES.items() for f in fs]


def test_version_file_matches():
    assert (ROOT / "VERSION").read_text().strip() == SCHEMA_VERSION == "0.1.0"


def test_every_shared_model_has_a_fixture():
    assert set(FIXTURES) == set(SHARED_MODELS)


@pytest.mark.parametrize("name,fixture", cases())
def test_fixture_valid_against_json_schema_and_pydantic(name, fixture):
    data = json.loads((ROOT / "fixtures" / fixture).read_text())
    jsonschema.validate(data, schema(name))                  # canonical JSON contract
    model = SHARED_MODELS[name].model_validate(data)         # Python implementation
    # Round trip: what Pydantic emits must still satisfy the canonical schema.
    jsonschema.validate(json.loads(model.model_dump_json()), schema(name))


@pytest.mark.parametrize("name", sorted(SHARED_MODELS))
def test_committed_schema_matches_models(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen", ROOT / "generate_schemas.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert schema(name) == json.loads(json.dumps(gen.build(name, SHARED_MODELS[name]), sort_keys=True)), \
        "schema drift: run packages/contracts/generate_schemas.py (and coordinate with Person 2)"


def test_network_access_true_rejected_everywhere():
    data = json.loads((ROOT / "fixtures/invalid/contract.network-true.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema("mission-contract"))
    with pytest.raises(ValidationError):
        SHARED_MODELS["mission-contract"].model_validate(data)


def test_request_missing_run_id_rejected_everywhere():
    data = json.loads((ROOT / "fixtures/invalid/tool-request.missing-run.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema("tool-request"))
    with pytest.raises(ValidationError):
        SHARED_MODELS["tool-request"].model_validate(data)


def test_extra_fields_rejected():
    data = json.loads((ROOT / "fixtures/contract.json").read_text())
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        SHARED_MODELS["mission-contract"].model_validate(data)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema("mission-contract"))


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../b", "a\\b", "%2e%2e/x", "C:/x"])
def test_contract_patterns_reject_unsafe_paths(bad):
    data = json.loads((ROOT / "fixtures/contract.json").read_text())
    data["allowed_paths"] = [bad]
    with pytest.raises(ValidationError):
        SHARED_MODELS["mission-contract"].model_validate(data)
