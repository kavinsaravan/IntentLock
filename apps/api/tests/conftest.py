"""Store fixtures.

  memory           - LocalMemoryStore (local fallback)
  dynamodb-moto    - DynamoStore against `moto` (MOCKED AWS; in-process emulation, not real AWS)
  dynamodb-real    - DynamoStore against a REAL table; only runs when INTENTLOCK_AWS_TEST_TABLE is set
                     (uses your ambient AWS credentials; creates only uniquely-named test items)
"""
import os

import boto3
import pytest
from moto import mock_aws

from intentlock_api.dynamo_store import TABLE_DEFINITION, DynamoStore
from intentlock_api.store import LocalMemoryStore

PARAMS = ["memory", "dynamodb-moto"] + (["dynamodb-real"] if os.environ.get("INTENTLOCK_AWS_TEST_TABLE") else [])


@pytest.fixture
def moto_env(monkeypatch):
    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")       # fake: guarantees moto, never real AWS
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture
def moto_store(moto_env):
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(TableName="il-test", **TABLE_DEFINITION)
        yield DynamoStore("il-test", region="us-east-1")


@pytest.fixture(params=PARAMS)
def store(request, monkeypatch):
    if request.param == "memory":
        yield LocalMemoryStore()
    elif request.param == "dynamodb-moto":
        request.getfixturevalue("moto_env")
        with mock_aws():
            boto3.client("dynamodb", region_name="us-east-1").create_table(TableName="il-test", **TABLE_DEFINITION)
            yield DynamoStore("il-test", region="us-east-1")
    else:
        yield DynamoStore(os.environ["INTENTLOCK_AWS_TEST_TABLE"], region=os.environ.get("AWS_REGION"))
