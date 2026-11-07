import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from intentlock_policy.models import MissionContract, Run

FIX = pathlib.Path(__file__).resolve().parents[2] / "packages" / "contracts" / "fixtures"
NOW = datetime(2026, 11, 7, 10, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def contract():
    return MissionContract.model_validate(json.loads((FIX / "contract.json").read_text()))


@pytest.fixture
def run():
    return Run.model_validate(json.loads((FIX / "run.json").read_text()))


@pytest.fixture
def make_req():
    def _make(tool, args, **over):
        r = {"request_id": "req_t", "run_id": "run_01", "contract_id": "contract_01",
             "contract_version": 1, "tool": tool, "arguments": args,
             "agent_id": "coding-agent-01", "timestamp": "2026-11-07T10:01:00Z"}
        r.update(over)
        return r
    return _make
