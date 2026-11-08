"""Guard tests: the suite must be unable to use real AWS credentials (see conftest.py)."""
import os

import boto3
import pytest

REAL_AWS = bool(os.environ.get("INTENTLOCK_AWS_TEST_TABLE"))   # same opt-in as conftest.py

pytestmark = pytest.mark.skipif(REAL_AWS, reason="real-table opt-in explicitly allows real credentials")


def test_ambient_credentials_resolve_to_fakes_only():
    creds = boto3.session.Session().get_credentials()
    assert creds is not None and creds.access_key == "testing" and creds.secret_key == "testing"
    assert creds.token is None


def test_no_profile_or_config_files_are_reachable():
    assert os.environ["AWS_CONFIG_FILE"] == os.devnull and os.environ["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
    assert boto3.session.Session().available_profiles == []
    for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        assert k not in os.environ
    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
