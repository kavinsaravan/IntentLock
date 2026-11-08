"""Read-only access to the exact patch artifact named by an ImpactReport, with integrity verification.

The execution plane (TwinGuard) WRITES artifacts using the PRD key layout (runs/{run_id}/patches/{sha256}.patch).
The control plane only READS them, and never serves bytes whose SHA-256 differs from the report's
`patch_sha256`. LOCAL DEV reader: a directory (INTENTLOCK_ARTIFACT_DIR) standing in for the S3 bucket.
An S3 reader is NOT implemented (no AWS access has been used); it must implement the same interface.
"""
import hashlib
import os
from pathlib import Path
from typing import Protocol

from intentlock_policy.paths import PathError, canonicalize_path

MAX_PATCH_BYTES = 2_000_000


class PatchUnavailable(Exception):
    pass


class PatchHashMismatch(Exception):
    pass


class ArtifactReader(Protocol):
    def read(self, key: str) -> bytes: ...


class LocalDirArtifactReader:
    label = "local-directory (NOT S3)"

    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        root = env.get("INTENTLOCK_ARTIFACT_DIR")
        return cls(root) if root else None

    def read(self, key: str) -> bytes:
        try:
            rel = canonicalize_path(key)
        except PathError as e:
            raise PatchUnavailable("invalid artifact key") from e
        path = (self._root / rel).resolve()
        if self._root not in path.parents:           # symlink / traversal escape
            raise PatchUnavailable("artifact outside root")
        try:
            if not path.is_file() or path.stat().st_size > MAX_PATCH_BYTES:
                raise PatchUnavailable("artifact missing or too large")
            return path.read_bytes()
        except OSError as e:
            raise PatchUnavailable(type(e).__name__) from e


def read_verified_patch(reader: ArtifactReader | None, key: str, expected_sha256: str) -> bytes:
    """Return the artifact bytes only if their SHA-256 equals the report's digest."""
    if reader is None:
        raise PatchUnavailable("no artifact reader configured")
    data = reader.read(key)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise PatchHashMismatch(key)
    return data
