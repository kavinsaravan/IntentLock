"""Client for Person 2's patch promoter (the ONLY component that may apply a patch to the real repository).

The control plane decides whether a promotion may be REQUESTED (an approved review exists and still matches);
the promoter independently re-verifies the base commit and the patch digest and applies exactly that artifact.
This client never applies anything itself. Transport: HTTP to INTENTLOCK_PROMOTER_URL (localhost http, otherwise
https), no redirects, bearer-less header X-IntentLock-Promoter-Token from INTENTLOCK_PROMOTER_TOKEN.

PROPOSED contract (not frozen until Person 2 agrees), POST {url}/promote:
  request  = PromoterRequest   response = PromoterResponse (below)
"""
import json
import os
import urllib.error
import urllib.request
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PromoterReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: str
    reviewer: str
    reviewed_at: str
    decision: Literal["approve"]


class PromoterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    impact_report_id: str
    run_id: str
    contract_id: str
    contract_version: int
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    patch_s3_key: str
    review: PromoterReview


class PromoterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["promoted", "refused"]
    base_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    applied_patch_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason_code: str | None = Field(default=None, max_length=64)
    message: str | None = Field(default=None, max_length=300)


class PromoterError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # refuse to follow redirects
        return None


class PromoterClient:
    def __init__(self, url: str, token: str, timeout: float = 30.0) -> None:
        u = urlparse(url)
        local = u.hostname in ("127.0.0.1", "localhost", "::1")
        if u.scheme != "https" and not (u.scheme == "http" and local):
            raise ValueError("promoter URL must be https (http only for localhost)")
        self._url, self._token, self._timeout = url.rstrip("/") + "/promote", token, timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        url, tok = env.get("INTENTLOCK_PROMOTER_URL"), env.get("INTENTLOCK_PROMOTER_TOKEN")
        return cls(url, tok) if url and tok else None

    def promote(self, req: PromoterRequest) -> PromoterResponse:
        data = req.model_dump_json().encode()
        r = urllib.request.Request(self._url, data=data, method="POST", headers={
            "content-type": "application/json", "X-IntentLock-Promoter-Token": self._token})
        try:
            with self._opener.open(r, timeout=self._timeout) as resp:
                body: Any = json.loads(resp.read(65536))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            raise PromoterError(type(e).__name__) from e
        try:
            return PromoterResponse.model_validate(body)
        except ValidationError as e:
            raise PromoterError("malformed promoter response") from e

    def finalize_rejection(self, impact_report_id: str, run_id: str) -> None:
        """Ask the artifact-owning service to finalize a rejected run's audit."""
        data = json.dumps({"impact_report_id": impact_report_id, "run_id": run_id}).encode()
        r = urllib.request.Request(self._url.removesuffix("/promote") + "/finalize-rejection",
                                   data=data, method="POST", headers={
                                       "content-type": "application/json",
                                       "X-IntentLock-Promoter-Token": self._token})
        try:
            with self._opener.open(r, timeout=self._timeout) as resp:
                body: Any = json.loads(resp.read(4096))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            raise PromoterError(type(e).__name__) from e
        if not isinstance(body, dict) or body.get("status") not in ("finalized", "queued"):
            raise PromoterError("audit finalization refused")
