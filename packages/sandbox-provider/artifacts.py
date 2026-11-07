"""Bounded, immutable local/S3 evidence objects for the frozen TwinGuard demo."""
import base64
import hashlib
import os
from pathlib import Path
import re

MAX_OBJECT_BYTES = 2_000_000
MAX_ARCHIVE_BYTES = 100_000_000
KEY = re.compile(r"runs/[A-Za-z0-9_-]{1,100}/(?:patches/[0-9a-f]{64}\.patch|tests/[A-Za-z0-9_-]{1,100}\.txt|impact/[A-Za-z0-9_-]{1,100}\.json|audit/final\.json|input/repository\.tar\.gz)\Z")


class ArtifactError(Exception):
    pass


def check_key(key):
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ArtifactError('INVALID_ARTIFACT_KEY')
    return key


def limit_for_key(key):
    return MAX_ARCHIVE_BYTES if key.endswith('/input/repository.tar.gz') else MAX_OBJECT_BYTES


def check_data(data, key=None):
    if not isinstance(data, bytes) or len(data) > (limit_for_key(key) if key else MAX_OBJECT_BYTES):
        raise ArtifactError('INVALID_ARTIFACT_SIZE')
    return hashlib.sha256(data).hexdigest()


class LocalArtifacts:
    label = 'local-directory'

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def put(self, key, data):
        check_key(key)
        digest = check_data(data, key)
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root not in path.resolve().parents:
            raise ArtifactError('ARTIFACT_OUTSIDE_ROOT')
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as file:
                file.write(data)
        except FileExistsError:
            if self.read(key) != data:
                raise ArtifactError('ARTIFACT_CONFLICT') from None
        return digest

    def read(self, key):
        check_key(key)
        path = self.root / key
        if self.root not in path.resolve().parents:
            raise ArtifactError('ARTIFACT_OUTSIDE_ROOT')
        try:
            if not path.is_file() or path.stat().st_size > limit_for_key(key):
                raise ArtifactError('ARTIFACT_UNAVAILABLE')
            data = path.read_bytes()
            check_data(data, key)
            return data
        except OSError:
            raise ArtifactError('ARTIFACT_UNAVAILABLE') from None


class S3Artifacts:
    label = 's3'

    def __init__(self, bucket, *, region='us-east-1', profile=None, client=None):
        if not isinstance(bucket, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
            raise ArtifactError('INVALID_BUCKET')
        self.bucket = bucket
        if client is None:
            import boto3
            client = boto3.Session(profile_name=profile, region_name=region).client('s3')
        self.client = client

    def put(self, key, data):
        check_key(key)
        digest = check_data(data, key)
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentLength=len(data),
                                   IfNoneMatch='*', ServerSideEncryption='AES256',
                                   ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
                                   Metadata={'sha256': digest})
        except Exception as exc:
            code = getattr(exc, 'response', {}).get('Error', {}).get('Code', '')
            if code in ('PreconditionFailed', '412'):
                if self.read(key) == data:
                    return digest
                raise ArtifactError('ARTIFACT_CONFLICT') from None
            raise ArtifactError('ARTIFACT_WRITE_FAILED') from None
        return digest

    def read(self, key):
        check_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key, ChecksumMode='ENABLED')
            body = response['Body']
            try:
                if response.get('ContentLength', limit_for_key(key) + 1) > limit_for_key(key):
                    raise ArtifactError('ARTIFACT_TOO_LARGE')
                data = body.read(limit_for_key(key) + 1)
                digest = check_data(data, key)
                if response.get('Metadata', {}).get('sha256') != digest:
                    raise ArtifactError('ARTIFACT_HASH_MISMATCH')
                checksum = response.get('ChecksumSHA256')
                if checksum and checksum != base64.b64encode(bytes.fromhex(digest)).decode():
                    raise ArtifactError('ARTIFACT_HASH_MISMATCH')
                return data
            finally:
                body.close()
        except ArtifactError:
            raise
        except Exception:
            raise ArtifactError('ARTIFACT_UNAVAILABLE') from None


def from_env(env=None, *, client=None):
    env = os.environ if env is None else env
    bucket = env.get('INTENTLOCK_ARTIFACT_BUCKET')
    if bucket:
        return S3Artifacts(bucket, region=env.get('AWS_REGION', 'us-east-1'),
                           profile=env.get('INTENTLOCK_AWS_PROFILE'), client=client)
    root = env.get('INTENTLOCK_ARTIFACT_DIR')
    if root:
        return LocalArtifacts(root)
    raise ArtifactError('ARTIFACT_STORE_NOT_CONFIGURED')
