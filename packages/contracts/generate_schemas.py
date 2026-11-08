"""Regenerate packages/contracts/schemas/*.schema.json from the Pydantic models.

The committed schema files are the canonical cross-language contract. Run:
    PYTHONPATH=packages/policy-engine python3 packages/contracts/generate_schemas.py
tests/contract fails if the committed files drift from the models.
"""
import json
import pathlib

from intentlock_policy.models import SCHEMA_VERSION, SHARED_MODELS

OUT = pathlib.Path(__file__).parent / "schemas"


def build(name, model):
    s = model.model_json_schema(mode="validation")
    s["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    s["$id"] = f"https://intentlock.dev/schemas/{SCHEMA_VERSION}/{name}.schema.json"
    s["x-schema-version"] = SCHEMA_VERSION
    return s


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, model in SHARED_MODELS.items():
        (OUT / f"{name}.schema.json").write_text(json.dumps(build(name, model), indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(SHARED_MODELS)} schemas for version {SCHEMA_VERSION}")
