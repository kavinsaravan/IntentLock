# Shared contracts — schema version 0.1.0 (PROPOSED; pending Person 2 agreement)

Canonical, language-neutral contract between the control plane (Python/Pydantic) and the execution plane.

- `schemas/*.schema.json` — JSON Schema (2020-12), one per type: mission-contract, mission-contract-draft, tool-request, authorization-decision, run, event, impact-report, review-approval.
- `fixtures/*.json` — valid examples. `fixtures/invalid/*.json` — must be rejected.
- `generate_schemas.py` — regenerates schemas from `packages/policy-engine/intentlock_policy/models.py`. `tests/contract` fails if committed schemas drift from the models or if any fixture stops validating under BOTH JSON Schema and Pydantic.

Validate from any language with a JSON Schema validator, e.g. Node: `ajv` against `schemas/tool-request.schema.json`. All objects are `additionalProperties: false`.

Changing a schema = breaking-change discussion with the other workstream first.
