"""TwinGuard-facing control-plane API: impact reports, exact-patch review, approval, promotion requests.

Division of responsibility (security-critical):
  * Execution plane (Person 2) produces ImpactReports + the patch artifact and performs the actual promotion.
  * This module (control plane) is the AUTHORITY on review state: the BACKEND decides whether a report can be
    approved or a promotion requested. The frontend is never trusted.
  * A blocked report is never approvable or promotable; "pass" never triggers anything by itself; promotion
    requires an approval bound to the exact patch SHA-256 and base commit, and is only executed by the promoter,
    which re-verifies both independently.
"""
import os
import hmac
import json
import uuid
from datetime import datetime
from typing import Any, Callable, Literal

from fastapi import Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from intentlock_policy.models import ImpactReport, ReviewApproval
from intentlock_policy.redact import sanitize_details

from .artifacts import PatchHashMismatch, PatchUnavailable, read_verified_patch
from .promoter import PromoterError, PromoterRequest, PromoterReview
from .store import ImpactExists


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer: str = Field(min_length=1, max_length=128)
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    comment: str | None = Field(default=None, max_length=500)


class PromoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requested_by: str = Field(min_length=1, max_length=128)
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PromotionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    impact_report_id: str
    status: Literal["requested", "promoted", "refused", "failed"]
    requested_by: str
    requested_at: str
    completed_at: str | None = None
    review_id: str
    patch_sha256: str
    base_commit: str
    reason_code: str | None = None
    message: str | None = None
    applied_patch_sha256: str | None = None


def reviewers_from_env(env=None) -> set[str] | None:
    env = os.environ if env is None else env
    raw = env.get("INTENTLOCK_REVIEWERS", "")
    names = {n.strip() for n in raw.split(",") if n.strip()}
    return names or None


MIN_TOKEN_LEN = 16
_PLACEHOLDER_MARKERS = ("replace-with", "change-me", "changeme", "example", "password", "secret-token")


def reviewer_tokens_from_env(env=None) -> dict[str, str]:
    """Parse INTENTLOCK_REVIEWER_TOKENS. Fails closed at startup on malformed, weak, placeholder or shared tokens."""
    env = os.environ if env is None else env
    try:
        data = json.loads(env.get("INTENTLOCK_REVIEWER_TOKENS", "{}"))
        if not isinstance(data, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in data.items()):
            raise ValueError("invalid reviewer token map")
    except (ValueError, TypeError) as e:
        raise ValueError("INTENTLOCK_REVIEWER_TOKENS must be a JSON object of nonempty reviewer names and tokens") from e
    for name, tok in data.items():
        if len(tok) < MIN_TOKEN_LEN or any(m in tok.lower() for m in _PLACEHOLDER_MARKERS):
            raise ValueError(f"reviewer token for {name!r} is too short (<{MIN_TOKEN_LEN}) or looks like a placeholder; "
                             "generate one with: openssl rand -hex 24")
    if len(set(data.values())) != len(data):
        raise ValueError("reviewer tokens must be unique per reviewer (a shared token defeats identity binding)")
    return data


def register(app, store, artifacts, promoter, require_internal: Callable[[str | None], None],
             now: Callable[[], datetime], reviewers: set[str] | None,
             reviewer_tokens: dict[str, str]) -> Callable[[str], dict]:
    """Register the impact routes; returns the read-only review/promotion status function they use."""
    # ------------------------------------------------------------------ helpers
    def report_or_404(impact_id: str) -> ImpactReport:
        r = store.get_impact(impact_id)
        if r is None:
            raise HTTPException(404, "IMPACT_REPORT_NOT_FOUND")
        return r

    def authorize_reviewer(name: str, supplied_token: str | None) -> None:
        expected = reviewer_tokens.get(name)
        # Compare as bytes: hmac.compare_digest raises TypeError on non-ASCII str, which would surface as a 500.
        if not expected or not supplied_token or not hmac.compare_digest(supplied_token.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(401, "REVIEWER_NOT_AUTHENTICATED")
        if reviewers is not None and name not in reviewers:
            raise HTTPException(403, "REVIEWER_NOT_AUTHORIZED")

    def patch_blocker(report: ImpactReport) -> str | None:
        try:
            read_verified_patch(artifacts, report.patch_s3_key, report.patch_sha256)
            return None
        except PatchHashMismatch:
            return "PATCH_HASH_MISMATCH"
        except PatchUnavailable:
            return "PATCH_UNAVAILABLE"

    def common_blockers(report: ImpactReport, *, check_artifact: bool) -> list[str]:
        b: list[str] = []
        run = store.get_run(report.run_id)
        contract = store.get_contract(report.contract_id, report.contract_version)
        if run is None:
            b.append("RUN_NOT_FOUND")
        else:
            if run.status in ("blocked", "failed", "stopped"):
                b.append("RUN_NOT_ELIGIBLE")
            if run.base_commit != report.base_commit:
                b.append("STALE_BASE_COMMIT")
        if contract is None:
            b.append("CONTRACT_NOT_FOUND")
        elif contract.status != "active":
            b.append("CONTRACT_NOT_ACTIVE")
        if report.decision == "block":
            b.append("IMPACT_BLOCKED")
        if check_artifact:
            pb = patch_blocker(report)
            if pb:
                b.append(pb)
        return b

    def approve_blockers(report: ImpactReport) -> list[str]:
        b = common_blockers(report, check_artifact=True)
        if not reviewer_tokens:
            b.append("REVIEW_AUTH_NOT_CONFIGURED")
        if store.get_final_review(report.impact_report_id) is not None:
            b.insert(0, "ALREADY_REVIEWED")
        return b

    def promote_blockers(report: ImpactReport) -> list[str]:
        b = common_blockers(report, check_artifact=True)
        if not reviewer_tokens:
            b.append("REVIEW_AUTH_NOT_CONFIGURED")
        fin = store.get_final_review(report.impact_report_id)
        if fin is None or fin.decision != "approve":
            b.insert(0, "NOT_APPROVED")
        elif fin.patch_sha256 != report.patch_sha256 or fin.base_commit != report.base_commit:
            b.insert(0, "PATCH_HASH_MISMATCH")
        if store.get_promotion(report.impact_report_id) is not None:
            b.insert(0, "ALREADY_REQUESTED")
        if promoter is None:
            b.append("PROMOTER_NOT_CONFIGURED")
        return b

    def event(report: ImpactReport, kind: str, message: str, details: dict[str, Any]) -> None:
        store.append_event(report.run_id, event_type=kind, status="recorded", message=message,
                           details=sanitize_details({"impact_report_id": report.impact_report_id, **details}))

    # ---------------------------------------------------------------- ingestion
    @app.post("/internal/impact", status_code=201)
    def ingest_impact(report: ImpactReport, response: Response,
                      x_intentlock_internal_token: str | None = Header(default=None)) -> dict:
        """Execution plane submits a TwinGuard ImpactReport (schema impact-report 0.1.0). Never triggers promotion."""
        require_internal(x_intentlock_internal_token)
        run = store.get_run(report.run_id)
        if run is None:
            raise HTTPException(404, "RUN_NOT_FOUND")
        if report.contract_id != run.contract_id or report.contract_version != run.contract_version:
            raise HTTPException(409, "IMPACT_MISMATCH")
        if report.base_commit != run.base_commit:
            raise HTTPException(409, "STALE_BASE_COMMIT")
        try:
            store.put_impact(report)
        except ImpactExists:
            existing = store.get_impact(report.impact_report_id)
            if existing == report:
                response.status_code = 200      # idempotent resubmission
                return {"impact_report_id": report.impact_report_id, "stored": False}
            raise HTTPException(409, "IMPACT_EXISTS")
        event(report, "impact_report", f"TwinGuard decision: {report.decision}",
              {"decision": report.decision, "patch_sha256": report.patch_sha256, "base_commit": report.base_commit})
        return {"impact_report_id": report.impact_report_id, "stored": True}

    # ------------------------------------------------------------------- reads
    @app.get("/api/impact/{impact_id}")
    def get_impact(impact_id: str) -> dict:
        return report_or_404(impact_id).model_dump(mode="json")

    @app.get("/api/impact/{impact_id}/patch")
    def get_patch(impact_id: str) -> Response:
        report = report_or_404(impact_id)
        try:
            data = read_verified_patch(artifacts, report.patch_s3_key, report.patch_sha256)
        except PatchHashMismatch:
            raise HTTPException(409, "PATCH_HASH_MISMATCH")
        except PatchUnavailable:
            raise HTTPException(404, "PATCH_UNAVAILABLE")
        return Response(content=data, media_type="text/plain; charset=utf-8",
                        headers={"X-Patch-SHA256": report.patch_sha256, "Cache-Control": "no-store"})

    @app.get("/api/impact/{impact_id}/status")
    def get_status(impact_id: str) -> dict:
        report = report_or_404(impact_id)
        fin = store.get_final_review(impact_id)
        promo = store.get_promotion(impact_id)
        ab, pb = approve_blockers(report), promote_blockers(report)
        return {
            "impact_report_id": impact_id, "decision": report.decision,
            "review": fin.model_dump(mode="json") if fin else None, "promotion": promo,
            "can_approve": not ab, "approve_blockers": ab,
            "can_promote": not pb, "promote_blockers": pb,
            "reviewer_policy": "authenticated reviewer" if reviewers is None else "authenticated allowlist",
            "promoter_configured": promoter is not None,
            "artifact_reader": getattr(artifacts, "label", None),
        }

    # ------------------------------------------------------------------ review
    def decide(impact_id: str, decision: Literal["approve", "reject"], body: ReviewBody, reviewer_token: str | None) -> dict:
        report = report_or_404(impact_id)
        authorize_reviewer(body.reviewer, reviewer_token)
        if body.patch_sha256 != report.patch_sha256:
            raise HTTPException(409, "PATCH_HASH_MISMATCH")
        if body.base_commit != report.base_commit:
            raise HTTPException(409, "STALE_BASE_COMMIT")
        existing = store.get_final_review(impact_id)
        if existing is not None:
            if existing.decision == decision and existing.reviewer == body.reviewer:
                if decision == "reject" and promoter is not None and hasattr(promoter, "finalize_rejection"):
                    try:
                        promoter.finalize_rejection(report.impact_report_id, report.run_id)
                    except PromoterError:
                        return {"review": existing.model_dump(mode="json"), "created": False, "audit_status": "unavailable"}
                return {"review": existing.model_dump(mode="json"), "created": False}
            raise HTTPException(409, "ALREADY_REVIEWED")
        if decision == "approve":
            blockers = approve_blockers(report)
            if blockers:
                raise HTTPException(409, blockers[0])
        run = store.get_run(report.run_id)
        if run is None:
            raise HTTPException(409, "RUN_NOT_FOUND")
        review = ReviewApproval(
            review_id=f"rev_{impact_id}", impact_report_id=impact_id, run_id=report.run_id,
            contract_id=report.contract_id, contract_version=report.contract_version, base_commit=report.base_commit,
            patch_sha256=report.patch_sha256, decision=decision, reviewer=body.reviewer, reviewed_at=now(),
            comment=body.comment)
        stored, created = store.put_final_review(review)
        if not created:
            if stored.decision == decision and stored.reviewer == body.reviewer:
                return {"review": stored.model_dump(mode="json"), "created": False}
            raise HTTPException(409, "ALREADY_REVIEWED")
        event(report, "review_decision", f"Patch {decision}d by {body.reviewer}" if decision == "approve" else f"Patch rejected by {body.reviewer}",
              {"review_id": stored.review_id, "decision": decision, "reviewer": body.reviewer,
               "patch_sha256": report.patch_sha256, "base_commit": report.base_commit,
               "contract_version": report.contract_version})
        if decision == "reject" and promoter is not None and hasattr(promoter, "finalize_rejection"):
            try:
                promoter.finalize_rejection(report.impact_report_id, report.run_id)
            except PromoterError:
                # The review is already durable. A retry of the same rejection retries export.
                return {"review": stored.model_dump(mode="json"), "created": True, "audit_status": "unavailable"}
        return {"review": stored.model_dump(mode="json"), "created": True}

    @app.post("/api/impact/{impact_id}/approve")
    def approve(impact_id: str, body: ReviewBody, x_intentlock_reviewer_token: str | None = Header(default=None)) -> dict:
        return decide(impact_id, "approve", body, x_intentlock_reviewer_token)

    @app.post("/api/impact/{impact_id}/reject")
    def reject(impact_id: str, body: ReviewBody, x_intentlock_reviewer_token: str | None = Header(default=None)) -> dict:
        return decide(impact_id, "reject", body, x_intentlock_reviewer_token)

    # --------------------------------------------------------------- promotion
    @app.post("/api/impact/{impact_id}/promote")
    def promote(impact_id: str, body: PromoteBody, x_intentlock_reviewer_token: str | None = Header(default=None)) -> dict:
        """Request promotion of the approved patch. The promoter (execution plane) does the real work."""
        report = report_or_404(impact_id)
        authorize_reviewer(body.requested_by, x_intentlock_reviewer_token)
        fin = store.get_final_review(impact_id)
        if fin is None or fin.decision != "approve":
            raise HTTPException(409, "NOT_APPROVED")
        if body.patch_sha256 != fin.patch_sha256:
            raise HTTPException(409, "PATCH_HASH_MISMATCH")
        if promoter is None:
            raise HTTPException(503, "PROMOTER_NOT_CONFIGURED")
        blockers = [x for x in promote_blockers(report) if x not in ("PROMOTER_NOT_CONFIGURED",)]
        if blockers:
            raise HTTPException(409, blockers[0])
        t = now().isoformat()
        rec = PromotionRecord(impact_report_id=impact_id, status="requested", requested_by=body.requested_by,
                              requested_at=t, review_id=fin.review_id, patch_sha256=fin.patch_sha256,
                              base_commit=fin.base_commit).model_dump(exclude_none=True)
        if not store.put_promotion(rec):                      # atomic: at most one promotion request per report
            raise HTTPException(409, "ALREADY_REQUESTED")
        req = PromoterRequest(impact_report_id=impact_id, run_id=report.run_id, contract_id=report.contract_id,
                              contract_version=report.contract_version, base_commit=fin.base_commit,
                              patch_sha256=fin.patch_sha256, patch_s3_key=report.patch_s3_key,
                              review=PromoterReview(review_id=fin.review_id, reviewer=fin.reviewer,
                                                    reviewed_at=fin.reviewed_at.isoformat(), decision="approve"))
        status, reason, message, applied = "failed", None, None, None
        try:
            res = promoter.promote(req)
            if res.status == "promoted":
                if res.applied_patch_sha256 != fin.patch_sha256 or res.base_commit != fin.base_commit:
                    # The promoter claims success for something other than what was approved: never report success.
                    status, reason, message = "failed", "PATCH_HASH_MISMATCH", "promoter result does not match the approved patch/base commit"
                else:
                    status, applied = "promoted", res.applied_patch_sha256
            else:
                status, reason, message = "refused", res.reason_code or "PROMOTION_REFUSED", res.message
        except PromoterError as e:
            status, reason, message = "failed", "PROMOTER_UNAVAILABLE", str(e)[:100]
        done = store.transition_promotion(impact_id, ["requested"], status=status, completed_at=now().isoformat(),
                                          reason_code=reason, message=message, applied_patch_sha256=applied)
        event(report, "promotion", f"Promotion {status}", {
            "status": status, "reason_code": reason, "patch_sha256": fin.patch_sha256, "base_commit": fin.base_commit,
            "requested_by": body.requested_by, "review_id": fin.review_id})
        return {"promotion": PromotionRecord.model_validate(done or store.get_promotion(impact_id)).model_dump(mode="json", exclude_none=True)}

    return get_status
