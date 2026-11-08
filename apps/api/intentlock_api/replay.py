"""TwinGuard Security Replay: read-only reconstruction of a recorded run's security decisions.

Evidence sources, in precedence order:
  * "s3"            - the TwinGuard evidence bucket (INTENTLOCK_REPLAY_BUCKET), read with the execution plane's
                      hash-verifying S3 reader. Read-only: this module never writes, lists outside runs/, or
                      accepts an object key from a caller.
  * "local"         - the local artifact directory (INTENTLOCK_ARTIFACT_DIR) written by TwinGuard / export_audit.
  * "control-plane" - the live store, by run id only (in-memory stores do not survive an API restart).

Per run, `runs/{run_id}/audit/final.json` (export_audit.py) is the complete control-plane record. Without it, only
the TwinGuard artifacts (impact reports, patches, test output) exist, and the view says so. Nothing here
re-decides anything: authorization comes from recorded policy decisions, consequence from TwinGuard reports, and
eligibility from the control plane's recorded blockers. Missing records are reported as missing, never inferred.
"""
import hashlib
import importlib.util
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import HTTPException, Query, Response
from pydantic import ValidationError

from intentlock_policy.engine import BASELINE_PROTECTED
from intentlock_policy.models import Event, ImpactReport, MissionContract, ReviewApproval, Run
from intentlock_policy.paths import matches_protected
from intentlock_policy.redact import sanitize_details

from .artifacts import LocalDirArtifactReader, PatchUnavailable
from .impact import PromotionRecord
from .store import aws_region

RUN_ID_RX = re.compile(r"[A-Za-z0-9_-]{1,100}")
_KEY_RX = re.compile(r"runs/(?P<run>[A-Za-z0-9_-]{1,100})/(?:(?P<audit>audit/final\.json)"
                     r"|impact/(?P<impact>[A-Za-z0-9_-]{1,100})\.json|patches/(?P<patch>[0-9a-f]{64})\.patch"
                     r"|tests/(?P<test>[A-Za-z0-9_-]{1,100})\.txt)")
SOURCES = ("s3", "local", "control-plane")

MAX_RUNS = 100
MAX_LIST_KEYS = 10_000
MAX_KEYS_PER_RUN = 200
MAX_EVENTS = 2000
MAX_ATTEMPTS = 10
MAX_PATCH_DISPLAY = 200_000
MAX_TEST_EXCERPT = 3000
LISTING_TTL = 10.0
CACHE_ENTRIES = 512

# The execution plane's MCP server also refuses these, so they are withheld from displayed diffs in every run.
_ALWAYS_PROTECTED = (*BASELINE_PROTECTED, ".github/**", "infra/**", ".aws", ".ssh")

_SECRET_PATTERNS = [
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
]
REDACTED = "[REDACTED]"


class EvidenceUnavailable(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _sandbox_artifacts():
    """The execution plane's artifact module (hash-verifying S3 reader). Loaded lazily: only S3 replay needs it."""
    path = Path(__file__).resolve().parents[3] / "packages" / "sandbox-provider" / "artifacts.py"
    spec = importlib.util.spec_from_file_location("intentlock_replay_artifacts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------------------------- sources
class LocalEvidence:
    id = "local"

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.label = "local artifact directory"
        self._reader = LocalDirArtifactReader(str(self.root))

    def status(self) -> tuple[bool, str | None]:
        return (True, None) if self.root.is_dir() else (False, "INTENTLOCK_ARTIFACT_DIR does not exist")

    def list_keys(self) -> dict[str, list[str]]:
        runs = self.root / "runs"
        out: dict[str, list[str]] = {}
        if runs.is_symlink() or not runs.is_dir():
            return out
        with os.scandir(runs) as entries:
            names = sorted(e.name for e in entries if e.is_dir(follow_symlinks=False) and RUN_ID_RX.fullmatch(e.name))
        for name in names[:MAX_LIST_KEYS // 10]:
            keys = []
            for sub in ("audit", "impact", "patches", "tests"):
                d = runs / name / sub
                if d.is_symlink() or not d.is_dir():
                    continue
                with os.scandir(d) as files:
                    for f in files:
                        key = f"runs/{name}/{sub}/{f.name}"
                        if f.is_file(follow_symlinks=False) and _KEY_RX.fullmatch(key):
                            keys.append(key)
            out[name] = sorted(keys)[:MAX_KEYS_PER_RUN]
        return out

    def read(self, key: str) -> bytes:
        if not _KEY_RX.fullmatch(key):
            raise EvidenceUnavailable("INVALID_ARTIFACT_KEY")
        try:
            return self._reader.read(key)
        except PatchUnavailable:
            raise EvidenceUnavailable("ARTIFACT_UNAVAILABLE") from None


class S3Evidence:
    id = "s3"

    def __init__(self, bucket: str, *, profile: str | None, region: str, client: Any = None) -> None:
        self._mod = _sandbox_artifacts()
        self._store = self._mod.S3Artifacts(bucket, region=region, profile=profile, client=client)
        self.bucket = bucket
        self.label = f"S3 bucket {bucket}"

    def status(self) -> tuple[bool, str | None]:
        return True, None

    def list_keys(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        token, seen = None, 0
        while True:
            kw = {"Bucket": self.bucket, "Prefix": "runs/", "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            try:
                page = self._store.client.list_objects_v2(**kw)
            except Exception:
                raise EvidenceUnavailable("S3_LIST_FAILED") from None
            for obj in page.get("Contents", []):
                seen += 1
                m = _KEY_RX.fullmatch(obj.get("Key", ""))
                if m:
                    lst = out.setdefault(m["run"], [])
                    if len(lst) < MAX_KEYS_PER_RUN:
                        lst.append(obj["Key"])
            token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
            if not token or seen >= MAX_LIST_KEYS:
                return out

    def read(self, key: str) -> bytes:
        if not _KEY_RX.fullmatch(key):
            raise EvidenceUnavailable("INVALID_ARTIFACT_KEY")
        try:
            return self._store.read(key)
        except self._mod.ArtifactError as e:
            raise EvidenceUnavailable(str(e)[:64]) from None


def sources_from_env(env=None) -> tuple[list[Any], list[dict[str, Any]]]:
    """Returns (usable sources, notes about configured-but-unusable ones). Never uses the default AWS profile."""
    env = os.environ if env is None else env
    sources: list[Any] = []
    notes: list[dict[str, Any]] = []
    bucket = env.get("INTENTLOCK_REPLAY_BUCKET")
    if bucket:
        profile = env.get("INTENTLOCK_AWS_PROFILE") or env.get("AWS_PROFILE")
        if not profile or profile == "default":
            notes.append({"id": "s3", "label": f"S3 bucket {bucket}", "available": False,
                          "reason": "Set AWS_PROFILE to the IntentLock profile; the default profile is never used."})
        else:
            try:
                sources.append(S3Evidence(bucket, profile=profile, region=aws_region(env)))
            except Exception as e:
                notes.append({"id": "s3", "label": f"S3 bucket {bucket}", "available": False,
                              "reason": f"S3 reader could not start ({type(e).__name__})"})
    root = env.get("INTENTLOCK_ARTIFACT_DIR")
    if root:
        sources.append(LocalEvidence(root))
    return sources, notes


# --------------------------------------------------------------------------------------------- redaction
def make_redactor(secret_values: Iterable[str]) -> Callable[[str], tuple[str, int]]:
    """Replace configured secret values (internal/reviewer tokens) and high-confidence credential patterns."""
    exact = sorted({s for s in secret_values if isinstance(s, str) and len(s) >= 8}, key=len, reverse=True)

    def redact(text: str) -> tuple[str, int]:
        n = 0
        for s in exact:
            if s in text:
                n += text.count(s)
                text = text.replace(s, REDACTED)
        for rx in _SECRET_PATTERNS:
            text, k = rx.subn(REDACTED, text)
            n += k
        return text, n
    return redact


def _scrub(obj: Any, redact: Callable[[str], tuple[str, int]]) -> Any:
    if isinstance(obj, str):
        return redact(obj)[0]
    if isinstance(obj, dict):
        return {k: _scrub(v, redact) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, redact) for v in obj]
    return obj


def _protected(path: str, rules: Iterable[str]) -> bool:
    path = path.strip().lstrip("/")
    if not path:
        return False
    try:
        return any(matches_protected(r, path) for r in rules)
    except Exception:
        return True                      # an unmatchable path is treated as protected (fail closed)


_GIT_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def sanitize_patch(text: str, rules: Iterable[str], redact: Callable[[str], tuple[str, int]]) -> dict[str, Any]:
    """Display copy of a unified diff: protected files' hunks withheld, credentials redacted, files summarized.

    The text is untrusted; it is only ever returned as data (the dashboard renders it as escaped text)."""
    rules = list(rules)
    lines = text.split("\n")
    files: list[dict[str, Any]] = []
    withheld: list[str] = []
    out: list[str] = []
    cur: dict[str, Any] | None = None
    skipping = False
    in_hunk = False
    for line in lines:
        m = _GIT_HEADER.match(line)
        if m:
            path = m.group(2)
            cur = {"path": path, "status": "modified", "added": 0, "deleted": 0, "withheld": False}
            files.append(cur)
            in_hunk = False
            skipping = _protected(m.group(1), rules) or _protected(path, rules)
            if skipping:
                cur["withheld"] = True
                withheld.append(path)
            out.append(line)
            continue
        if cur is not None and not in_hunk:
            if line.startswith("new file mode"):
                cur["status"] = "created"
            elif line.startswith("deleted file mode"):
                cur["status"] = "deleted"
            elif line.startswith("rename to "):
                cur["status"] = "renamed"
                cur["path"] = line[10:]
                if not cur["withheld"] and _protected(cur["path"], rules):
                    cur["withheld"] = skipping = True
                    withheld.append(cur["path"])
        if line.startswith("@@"):
            in_hunk = True
        if cur is not None and in_hunk:
            if line.startswith("+") and not line.startswith("+++"):
                cur["added"] += 1
            elif line.startswith("-") and not line.startswith("---"):
                cur["deleted"] += 1
        if skipping:
            continue
        out.append(line)
    if not files:
        # Not a git diff: if any header names a protected path, withhold everything (cannot split safely).
        heads = [ln[4:].split("\t")[0] for ln in lines if ln.startswith(("--- ", "+++ "))]
        heads = [h[2:] if h.startswith(("a/", "b/")) else h for h in heads]
        if any(_protected(h, rules) for h in heads):
            return {"text": "", "files": [], "withheld_files": sorted(set(heads)), "redactions": 0, "parsed": False}
    redactions = 0
    clean: list[str] = []
    for line in out:
        r, n = redact(line)
        redactions += n
        clean.append(r)
    return {"text": "\n".join(clean), "files": files, "withheld_files": withheld, "redactions": redactions,
            "parsed": bool(files)}


# ----------------------------------------------------------------------------------------------- records
class Record:
    """Parsed evidence for one run from one source (shape of export_audit.py's final.json)."""

    def __init__(self, run_id: str, source_id: str, source_label: str) -> None:
        self.run_id, self.source_id, self.source_label = run_id, source_id, source_label
        self.kind = "twinguard_artifacts"           # | "audit_export" | "live_store"
        self.run: Run | None = None
        self.contract: MissionContract | None = None
        self.events: list[Event] | None = None       # None = control-plane events unavailable
        self.events_truncated = False
        self.impacts: list[tuple[ImpactReport, dict[str, Any] | None]] = []
        self.audit: dict[str, Any] | None = None
        self.limitations: list[str] = []
        self.reader: Callable[[str], bytes] | None = None

    @property
    def empty(self) -> bool:
        return self.run is None and not self.impacts


def _parse_audit(data: Any, run_id: str, rec: Record) -> None:
    if not isinstance(data, dict) or not all(k in data for k in ("run", "contract", "events", "impacts")):
        raise ValueError("audit shape")
    run = Run.model_validate(data["run"])
    if run.run_id != run_id:
        raise ValueError("audit run mismatch")
    contract = MissionContract.model_validate(data["contract"])
    if (contract.contract_id, contract.version) != (run.contract_id, run.contract_version):
        raise ValueError("audit contract mismatch")
    raw_events = data["events"]
    if not isinstance(raw_events, list):
        raise ValueError("audit events")
    events = [Event.model_validate(e) for e in raw_events[:MAX_EVENTS]]
    if any(e.run_id != run_id for e in events):
        raise ValueError("audit event run mismatch")
    impacts = []
    for item in data["impacts"][:MAX_ATTEMPTS]:
        report = ImpactReport.model_validate(item["report"])
        if report.run_id != run_id:
            raise ValueError("audit impact run mismatch")
        status = item.get("status")
        impacts.append((report, status if isinstance(status, dict) else None))
    rec.run, rec.contract, rec.events, rec.impacts = run, contract, events, impacts
    rec.events_truncated = len(raw_events) > MAX_EVENTS
    rec.kind = "audit_export"


# ------------------------------------------------------------------------------------------------ service
class Replay:
    def __init__(self, sources: list[Any], notes: list[dict[str, Any]], store: Any,
                 impact_status: Callable[[str], dict] | None, artifacts: Any,
                 secret_values: Iterable[str] = ()) -> None:
        self.sources = {s.id: s for s in sources}
        self.notes = notes
        self.store = store
        self.impact_status = impact_status
        self.artifacts = artifacts
        self.redact = make_redactor(secret_values)
        self._lock = threading.Lock()
        self._listing: dict[str, tuple[float, dict[str, list[str]]]] = {}
        self._json: dict[tuple[str, str], tuple[str, Any]] = {}

    # ---- source access (objects are write-once, so parsed JSON may be cached by key) ----
    def _keys(self, source) -> dict[str, list[str]]:
        with self._lock:
            hit = self._listing.get(source.id)
            if hit and time.monotonic() - hit[0] < LISTING_TTL:
                return hit[1]
        keys = source.list_keys()
        with self._lock:
            self._listing[source.id] = (time.monotonic(), keys)
        return keys

    def _read_json(self, source, key: str) -> tuple[str, Any, int]:
        with self._lock:
            hit = self._json.get((source.id, key))
        if hit:
            return hit[0], hit[1], hit[2]
        raw = source.read(key)
        digest = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)
        with self._lock:
            if len(self._json) >= CACHE_ENTRIES:
                self._json.clear()
            self._json[(source.id, key)] = (digest, data, len(raw))
        return digest, data, len(raw)

    # ---- record loading ----
    def _from_source(self, source, run_id: str, keys: list[str]) -> Record:
        rec = Record(run_id, source.id, source.label)
        rec.reader = source.read
        audit_key = f"runs/{run_id}/audit/final.json"
        if audit_key in keys:
            try:
                digest, data, size = self._read_json(source, audit_key)
                _parse_audit(data, run_id, rec)
                rec.audit = {"key": audit_key, "sha256": digest, "bytes": size, "source": source.label}
            except EvidenceUnavailable as e:
                rec.limitations.append(f"The final audit export could not be read ({e.code}).")
            except (ValueError, KeyError, TypeError, ValidationError):
                rec.limitations.append("The final audit export is malformed and was not used.")
        known = {r.impact_report_id for r, _ in rec.impacts}
        for key in keys:
            m = _KEY_RX.fullmatch(key)
            if not m or not m["impact"] or m["impact"] in known or len(rec.impacts) >= MAX_ATTEMPTS:
                continue
            try:
                _, data, _ = self._read_json(source, key)
                report = ImpactReport.model_validate(data)
                if report.run_id != run_id or report.impact_report_id != m["impact"]:
                    raise ValueError("impact identity mismatch")
            except EvidenceUnavailable as e:
                rec.limitations.append(f"Impact report {m['impact']} could not be read ({e.code}).")
                continue
            except (ValueError, KeyError, TypeError, ValidationError):
                rec.limitations.append(f"Impact report {m['impact']} is malformed and was not used.")
                continue
            rec.impacts.append((report, None))
            known.add(report.impact_report_id)
        rec.impacts.sort(key=lambda x: x[0].created_at)       # attempt order; export_audit sorts by id
        if rec.kind == "twinguard_artifacts" and rec.impacts:
            rec.limitations.append("No final audit export exists for this run in this evidence store. Only TwinGuard "
                                   "artifacts are available: authorization decisions, human review and promotion "
                                   "records are not part of this evidence.")
        return rec

    def _from_store(self, run_id: str) -> Record | None:
        run = self.store.get_run(run_id)
        if run is None:
            return None
        backend = getattr(self.store, "backend", "store")
        rec = Record(run_id, "control-plane", f"live control-plane store ({backend})")
        rec.kind = "live_store"
        rec.run = run
        rec.contract = self.store.get_contract(run.contract_id, run.contract_version)
        events = self.store.list_events(run_id)
        rec.events, rec.events_truncated = events[:MAX_EVENTS], len(events) > MAX_EVENTS
        ids: list[str] = []
        for e in rec.events:
            iid = (e.details or {}).get("impact_report_id") if e.event_type == "impact_report" else None
            if isinstance(iid, str) and iid not in ids:
                ids.append(iid)
        for iid in ids[:MAX_ATTEMPTS]:
            report = self.store.get_impact(iid)
            if report is None:
                rec.limitations.append(f"Impact report {iid} is referenced by an event but missing from the store.")
                continue
            status = None
            if self.impact_status is not None:
                try:
                    status = self.impact_status(iid)
                except HTTPException:
                    status = None
            rec.impacts.append((report, status))
        rec.impacts.sort(key=lambda x: x[0].created_at)
        if self.artifacts is not None:
            rec.reader = self.artifacts.read
        if "NOT" in backend:
            rec.limitations.append("This record is read from the in-memory control-plane store. It is not persisted "
                                   "and disappears when the API restarts; export the audit to keep it.")
        rec.limitations.append("This view is computed from the live control-plane store, not from a final audit export.")
        return rec

    def _find(self, run_id: str, source: str) -> Record | None:
        order = SOURCES if source == "auto" else (source,)
        partial: Record | None = None
        for sid in order:
            if sid == "control-plane":
                # Live control-plane records (events, review, promotion) supersede artifact-only evidence.
                rec = self._from_store(run_id)
                if rec is not None:
                    return rec
                continue
            src = self.sources.get(sid)
            if src is None:
                continue
            try:
                keys = self._keys(src).get(run_id)
            except EvidenceUnavailable:
                continue
            if not keys:
                continue
            rec = self._from_source(src, run_id, keys)
            if rec.kind == "audit_export":
                return rec
            if not rec.empty and partial is None:
                partial = rec
            if source != "auto":
                return rec if not rec.empty else None
        return partial

    # ---- API ----
    def catalog(self) -> dict[str, Any]:
        source_info: list[dict[str, Any]] = list(self.notes)
        work: list[tuple[Any, str, list[str]]] = []
        for src in self.sources.values():
            ok, reason = src.status()
            info = {"id": src.id, "label": src.label, "available": ok, "reason": reason, "runs": 0}
            if ok:
                try:
                    listing = self._keys(src)
                except EvidenceUnavailable as e:
                    info.update(available=False, reason=e.code)
                    listing = {}
                recorded = {r: k for r, k in listing.items()
                            if any(_KEY_RX.fullmatch(x)["audit"] or _KEY_RX.fullmatch(x)["impact"] for x in k)}
                info["runs"] = len(recorded)
                work += [(src, r, k) for r, k in recorded.items()]
            source_info.append(info)
        seen: set[str] = set()
        unique = []
        for src, r, k in sorted(work, key=lambda w: SOURCES.index(w[0].id)):
            if r not in seen:
                seen.add(r)
                unique.append((src, r, k))
        truncated = len(unique) > MAX_RUNS
        with ThreadPoolExecutor(max_workers=8) as pool:
            recs = list(pool.map(lambda w: self._from_source(*w), unique[:MAX_RUNS]))
        runs = [self._summary(rec) for rec in recs if not rec.empty]
        runs.sort(key=lambda s: s["last_activity"] or "", reverse=True)
        backend = getattr(self.store, "backend", "store")
        return _scrub({"sources": source_info, "runs": runs, "truncated": truncated,
                       "live_lookup": {"store": backend, "persistent": "NOT" not in backend}}, self.redact)

    def view(self, run_id: str, source: str = "auto") -> dict[str, Any] | None:
        rec = self._find(run_id, source)
        if rec is None or rec.empty:
            return None
        return _scrub(self._build(rec), self.redact)

    # ---- normalization ----
    def _summary(self, rec: Record) -> dict[str, Any]:
        attempts = [_attempt_core(rec, r, s) for r, s in rec.impacts]
        outcome = _outcome(rec, attempts)
        times = [t for t in (rec.run and (rec.run.completed_at or rec.run.stopped_at or rec.run.created_at),
                             *[r.created_at for r, _ in rec.impacts]) if t]
        return {"run_id": rec.run_id, "source": rec.source_id, "source_label": rec.source_label,
                "evidence": rec.kind, "status": rec.run.status if rec.run else None,
                "contract_id": rec.run.contract_id if rec.run else (rec.impacts[0][0].contract_id if rec.impacts else None),
                "contract_version": rec.run.contract_version if rec.run else (rec.impacts[0][0].contract_version if rec.impacts else None),
                "mission": rec.contract.mission[:160] if rec.contract else None,
                "attempts": len(rec.impacts), "outcome": outcome["code"], "outcome_title": outcome["title"],
                "last_activity": max(times).isoformat() if times else None}

    def _build(self, rec: Record) -> dict[str, Any]:
        rules = list(_ALWAYS_PROTECTED) + (list(rec.contract.protected_paths) if rec.contract else [])
        attempts = []
        for report, status in rec.impacts:
            a = _attempt_core(rec, report, status)
            a["patch"] = self._patch(rec, report, rules)
            a["tests"] = [self._test(rec, t) for t in report.tests]
            attempts.append(a)
        outcome = _outcome(rec, attempts)
        limitations = list(rec.limitations)
        if rec.events_truncated:
            limitations.append(f"Only the first {MAX_EVENTS} events are shown.")
        if rec.contract is None and rec.kind == "twinguard_artifacts":
            limitations.append("The mission contract is not part of the TwinGuard artifacts; only built-in protected "
                               "paths are withheld from the displayed diff.")
        run = rec.run
        return {
            "run_id": rec.run_id,
            "evidence": {"kind": rec.kind, "source": rec.source_id, "source_label": rec.source_label,
                         "audit": rec.audit, "events_available": rec.events is not None},
            "run": None if run is None else {
                k: (v.isoformat() if hasattr(v, "isoformat") else v)
                for k, v in run.model_dump().items()
                if k in ("run_id", "contract_id", "contract_version", "status", "base_commit", "created_at",
                         "started_at", "stopped_at", "completed_at", "stop_reason")},
            "contract": None if rec.contract is None else rec.contract.model_dump(mode="json", include={
                "contract_id", "version", "mission", "repository_id", "base_commit", "allowed_tools", "allowed_paths",
                "protected_paths", "approved_tests", "limits", "required_exports", "created_by", "approved_at",
                "status"}),
            "timeline": _timeline(rec),
            "authorization": _authorization_summary(rec),
            "attempts": attempts,
            "outcome": outcome,
            "claims": _claims(rec, attempts),
            "limitations": limitations,
        }

    def _patch(self, rec: Record, report: ImpactReport, rules: list[str]) -> dict[str, Any]:
        expected = f"runs/{rec.run_id}/patches/{report.patch_sha256}.patch"
        base = {"key": report.patch_s3_key, "sha256": report.patch_sha256, "base_commit": report.base_commit,
                "text": None, "files": [], "withheld_files": [], "redactions": 0, "truncated": False,
                "bytes": None, "parsed": False}
        if report.patch_s3_key != expected:
            return {**base, "status": "key_invalid"}
        if rec.reader is None:
            return {**base, "status": "unavailable", "reason": "No artifact reader is configured for this record."}
        try:
            raw = rec.reader(expected)
        except EvidenceUnavailable as e:
            st = "hash_mismatch" if "HASH" in e.code else "unavailable"
            return {**base, "status": st, "reason": e.code}
        except PatchUnavailable as e:
            return {**base, "status": "unavailable", "reason": str(e)[:64]}
        if hashlib.sha256(raw).hexdigest() != report.patch_sha256:
            return {**base, "status": "hash_mismatch", "reason": "Stored bytes do not hash to the reported digest."}
        shown = raw[:MAX_PATCH_DISPLAY]
        clean = sanitize_patch(shown.decode("utf-8", "replace"), rules, self.redact)
        return {**base, **clean, "status": "verified", "bytes": len(raw), "truncated": len(raw) > MAX_PATCH_DISPLAY}

    def _test(self, rec: Record, t) -> dict[str, Any]:
        out = {"test_id": t.test_id, "status": t.status, "exit_code": t.exit_code, "duration_ms": t.duration_ms,
               "output_key": t.output_s3_key, "output": None, "output_status": "not_recorded", "output_truncated": False}
        key = t.output_s3_key
        if not key:
            return out
        m = _KEY_RX.fullmatch(key)
        if not m or not m["test"] or m["run"] != rec.run_id:
            return {**out, "output_status": "key_invalid"}
        if rec.reader is None:
            return {**out, "output_status": "unavailable"}
        try:
            raw = rec.reader(key)
        except (EvidenceUnavailable, PatchUnavailable):
            return {**out, "output_status": "unavailable"}
        text = raw.decode("utf-8", "replace")
        excerpt = text[-MAX_TEST_EXCERPT:]
        return {**out, "output": self.redact(excerpt)[0], "output_status": "available",
                "output_truncated": len(text) > MAX_TEST_EXCERPT}


# ------------------------------------------------------------------------------------- pure derivations
def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _tool_events(rec: Record) -> list[Event]:
    return [e for e in (rec.events or []) if e.event_type == "tool_request"]


def _decision_status(e: Event) -> str:
    return "allowed" if e.decision == "allow" else "denied" if e.decision == "deny" else "pending"


def _authorization_summary(rec: Record) -> dict[str, Any]:
    if rec.events is None:
        return {"available": False, "total": 0, "allowed": 0, "denied": 0, "pending": 0, "denied_requests": []}
    tools = _tool_events(rec)
    denied = [e for e in tools if e.decision == "deny"]
    return {"available": True, "total": len(tools),
            "allowed": sum(e.decision == "allow" for e in tools), "denied": len(denied),
            "pending": sum(e.decision is None for e in tools),
            "denied_requests": [{"seq": e.seq, "tool": e.tool, "reason_code": e.reason_code, "request_id": e.request_id,
                                 "arguments": sanitize_details(e.arguments_redacted) if e.arguments_redacted else None,
                                 "message": e.message} for e in denied[:50]]}


def _impact_event_seq(rec: Record) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in rec.events or []:
        iid = (e.details or {}).get("impact_report_id") if e.event_type == "impact_report" else None
        if isinstance(iid, str) and iid not in out:
            out[iid] = e.seq
    return out


def _attempt_core(rec: Record, report: ImpactReport, status: dict[str, Any] | None) -> dict[str, Any]:
    # Check 1: the recorded policy decision for the propose_patch request that produced this report.
    if rec.events is None:
        auth = {"status": "unavailable", "basis": "Control-plane authorization events are not part of this evidence."}
    else:
        seq = _impact_event_seq(rec).get(report.impact_report_id)
        prior = [e for e in _tool_events(rec) if e.tool == "propose_patch" and seq is not None and e.seq < seq]
        chosen = ([e for e in prior if e.decision == "allow"] or prior)[-1:] if prior else []
        if seq is None:
            auth = {"status": "not_recorded", "basis": "No impact_report event references this report."}
        elif not chosen:
            auth = {"status": "not_recorded",
                    "basis": f"No propose_patch tool request precedes impact event #{seq} in the recorded events."}
        else:
            e = chosen[0]
            auth = {"status": _decision_status(e), "seq": e.seq, "request_id": e.request_id, "reason_code": e.reason_code,
                    "latency_ms": e.latency_ms, "message": e.message,
                    "basis": f"propose_patch request event #{e.seq}, the last before impact event #{seq}."}
    # Check 2: the TwinGuard report as recorded.
    failing = [t.test_id for t in report.tests if t.status != "passed"]
    blocking = [f.code for f in report.findings if f.severity in ("critical", "high")]
    consequence = {"status": {"block": "blocked", "review_required": "review_required", "pass": "passed"}[report.decision],
                   "decision": report.decision, "failing_tests": failing, "blocking_findings": blocking,
                   "tests_recorded": len(report.tests),
                   "basis": f"TwinGuard impact report {report.impact_report_id} ({report.sandbox_id})."}
    review, promotion, eligibility = _review_and_promotion(report, status)
    return {"impact_report_id": report.impact_report_id, "created_at": _iso(report.created_at),
            "sandbox_id": report.sandbox_id, "base_commit": report.base_commit, "patch_sha256": report.patch_sha256,
            "summary": report.summary.model_dump(), "findings": [f.model_dump() for f in report.findings],
            "authorization": auth, "consequence": consequence,
            "review": review, "promotion": promotion, "eligibility": eligibility}


def _review_and_promotion(report: ImpactReport, status: dict[str, Any] | None):
    blocked = report.decision == "block"
    if status is None:
        why = "Review and promotion records are not part of this evidence."
        review = {"status": "unavailable", "basis": why}
        promotion = {"status": "unavailable", "basis": why}
        eligibility = {"recorded": False, "promotable": False if blocked else None,
                       "approve_blockers": ["IMPACT_BLOCKED"] if blocked else [],
                       "promote_blockers": ["IMPACT_BLOCKED"] if blocked else [],
                       "basis": "TwinGuard decision only" if blocked else why}
        return review, promotion, eligibility
    rv_raw = status.get("review")
    rv = None
    if isinstance(rv_raw, dict):
        try:
            rv = ReviewApproval.model_validate(rv_raw)
        except ValidationError:
            rv = None
    if rv is None:
        review = {"status": "none" if rv_raw is None else "unavailable",
                  "basis": "No review decision was recorded." if rv_raw is None else "The recorded review is malformed."}
    else:
        review = {"status": "approved" if rv.decision == "approve" else "rejected", "review_id": rv.review_id,
                  "reviewer": rv.reviewer, "reviewed_at": _iso(rv.reviewed_at), "comment": rv.comment,
                  "patch_sha256": rv.patch_sha256, "base_commit": rv.base_commit, "contract_version": rv.contract_version,
                  "matches_patch": rv.patch_sha256 == report.patch_sha256 and rv.base_commit == report.base_commit,
                  "basis": f"Review record {rv.review_id}."}
    pr_raw = status.get("promotion")
    if pr_raw is None:
        promotion = {"status": "none", "basis": "No promotion was requested."}
    else:
        try:
            pr = PromotionRecord.model_validate(pr_raw)
        except ValidationError:
            pr = None
        if pr is None:
            promotion = {"status": "unavailable", "basis": "The recorded promotion is malformed."}
        else:
            approved = rv is not None and rv.decision == "approve"
            matches = (pr.applied_patch_sha256 == report.patch_sha256 and pr.patch_sha256 == report.patch_sha256
                       and pr.base_commit == report.base_commit)
            st = pr.status
            if st == "promoted" and not (approved and matches and not blocked):
                st = "inconsistent"      # a success record that does not match the approved patch is never shown as promoted
            promotion = {"status": st, "requested_by": pr.requested_by, "requested_at": pr.requested_at,
                         "completed_at": pr.completed_at, "reason_code": pr.reason_code, "message": pr.message,
                         "review_id": pr.review_id, "patch_sha256": pr.patch_sha256, "base_commit": pr.base_commit,
                         "applied_patch_sha256": pr.applied_patch_sha256,
                         "applied_matches_approved": matches if pr.status == "promoted" else None,
                         "basis": "Promotion record."}
    ab = [x for x in status.get("approve_blockers", []) if isinstance(x, str)][:20]
    pb = [x for x in status.get("promote_blockers", []) if isinstance(x, str)][:20]
    if blocked and "IMPACT_BLOCKED" not in pb:
        pb = ["IMPACT_BLOCKED", *pb]
    eligibility = {"recorded": True, "can_approve": bool(status.get("can_approve")) and not blocked,
                   "approve_blockers": ab, "can_promote": bool(status.get("can_promote")) and not blocked,
                   "promote_blockers": pb, "promotable": False if blocked else bool(status.get("can_promote")),
                   "basis": "Control-plane review state recorded with this evidence."}
    return review, promotion, eligibility


_STAGE_TITLES = {"run_started": "Run started", "run_completed": "Run finished", "run_stopped": "Run stopped",
                 "review_decision": "Human review", "promotion": "Promotion", "impact_report": "TwinGuard evaluation"}


def _timeline(rec: Record) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if rec.contract is not None:
        items.append({"seq": None, "timestamp": _iso(rec.contract.approved_at), "stage": "mission_approved",
                      "title": "Mission contract approved", "status": "approved",
                      "detail": f"{rec.contract.contract_id} v{rec.contract.version} approved by {rec.contract.created_by}",
                      "source": "mission contract record"})
    reports = {r.impact_report_id: r for r, _ in rec.impacts}
    if rec.events is None:
        for r, _ in sorted(rec.impacts, key=lambda x: x[0].created_at):
            items.append(_impact_item(None, _iso(r.created_at), r, "impact report artifact"))
        return items
    for e in sorted(rec.events, key=lambda x: x.seq):
        base = {"seq": e.seq, "timestamp": _iso(e.timestamp), "source": f"event #{e.seq}"}
        d = e.details or {}
        if e.event_type == "tool_request":
            items.append({**base, "stage": "tool_request", "title": f"{e.tool or 'unknown tool'} requested",
                          "status": _decision_status(e), "tool": e.tool, "reason_code": e.reason_code,
                          "request_id": e.request_id, "latency_ms": e.latency_ms, "detail": e.message,
                          "arguments": sanitize_details(e.arguments_redacted) if e.arguments_redacted else None,
                          "evidence": sanitize_details(e.evidence) if e.evidence else None})
        elif e.event_type == "impact_report":
            iid = d.get("impact_report_id")
            r = reports.get(iid) if isinstance(iid, str) else None
            if r is not None:
                items.append(_impact_item(e.seq, _iso(e.timestamp), r, f"event #{e.seq}"))
            else:
                items.append({**base, "stage": "twinguard", "title": "TwinGuard evaluation",
                              "status": {"block": "blocked", "pass": "passed", "review_required": "review_required"}.get(d.get("decision"), "recorded"),
                              "detail": e.message, "impact_report_id": iid if isinstance(iid, str) else None})
        elif e.event_type == "review_decision":
            dec = d.get("decision")
            items.append({**base, "stage": "review", "title": "Human review",
                          "status": "approved" if dec == "approve" else "rejected" if dec == "reject" else "recorded",
                          "detail": e.message, "impact_report_id": d.get("impact_report_id"),
                          "reviewer": d.get("reviewer"), "patch_sha256": d.get("patch_sha256")})
        elif e.event_type == "promotion":
            st = d.get("status")
            items.append({**base, "stage": "promotion", "title": "Promotion",
                          "status": st if st in ("promoted", "refused", "failed", "requested") else "recorded",
                          "detail": e.message, "impact_report_id": d.get("impact_report_id"),
                          "reason_code": d.get("reason_code"), "patch_sha256": d.get("patch_sha256")})
        elif e.event_type in ("run_completed", "run_stopped"):
            st = d.get("status")
            items.append({**base, "stage": "run_final", "title": _STAGE_TITLES[e.event_type],
                          "status": st if st in ("completed", "failed", "stopped", "blocked") else "recorded",
                          "detail": e.message})
        elif e.event_type == "run_started":
            items.append({**base, "stage": "run_started", "title": "Run started", "status": "recorded",
                          "detail": f"contract {d.get('contract_id', '?')} v{d.get('contract_version', '?')}"})
        else:
            msg = e.message or ""
            started = msg.startswith("Agent launcher started")
            items.append({**base, "stage": "agent_started" if started else "agent_note",
                          "title": "Agent started" if started else "Agent activity", "status": "recorded",
                          "detail": msg})
    return items


def _impact_item(seq: int | None, ts: str | None, r: ImpactReport, source: str) -> dict[str, Any]:
    return {"seq": seq, "timestamp": ts, "source": source, "stage": "twinguard", "title": "TwinGuard evaluation",
            "status": {"block": "blocked", "pass": "passed", "review_required": "review_required"}[r.decision],
            "detail": f"Impact report {r.impact_report_id}: decision {r.decision}", "impact_report_id": r.impact_report_id,
            "tests": [{"test_id": t.test_id, "status": t.status, "exit_code": t.exit_code} for t in r.tests],
            "findings": [{"severity": f.severity, "code": f.code} for f in r.findings]}


def _outcome(rec: Record, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline outcome; `impact_report_id` names the attempt the headline is about (None for run-level outcomes)."""
    def out(code, title, tone, detail, attempt=None):
        return {"code": code, "title": title, "tone": tone, "detail": detail,
                "impact_report_id": attempt["impact_report_id"] if attempt else None}
    promoted = [a for a in attempts if a["promotion"]["status"] == "promoted"]
    if promoted:
        a = promoted[-1]
        return out("safe_patch_promoted", "Safe patch promoted", "ok",
                   f"Impact report {a['impact_report_id']} passed TwinGuard, was approved by "
                   f"{a['review'].get('reviewer')}, and the promoter applied the same SHA-256.", a)
    failed = [a for a in attempts if a["promotion"]["status"] in ("refused", "failed", "inconsistent")]
    if failed:
        a = failed[-1]
        return out("promotion_failed", "Promotion not completed", "deny",
                   f"Promotion of {a['impact_report_id']} ended {a['promotion']['status']}"
                   f"{' (' + a['promotion']['reason_code'] + ')' if a['promotion'].get('reason_code') else ''}.", a)
    rejected = [a for a in attempts if a["review"]["status"] == "rejected"]
    blocked = [a for a in attempts if a["consequence"]["status"] == "blocked"]
    if rejected and (not blocked or attempts.index(rejected[-1]) > attempts.index(blocked[-1])):
        a = rejected[-1]
        return out("patch_rejected", "Patch rejected by reviewer", "stopped",
                   f"Impact report {a['impact_report_id']} was rejected by {a['review'].get('reviewer')}.", a)
    if blocked:
        a, latest = blocked[-1], attempts[-1]
        tail = ""
        if latest is not a:
            st = latest["consequence"]["status"]
            tail = (f" A later attempt ({latest['impact_report_id']}) "
                    f"{'passed TwinGuard' if st == 'passed' else 'requires review'}")
            tail += "; its review evidence is unavailable." if latest["review"]["status"] == "unavailable" else "."
        return out("unsafe_patch_blocked", "Unsafe patch blocked", "deny",
                   f"TwinGuard blocked {a['impact_report_id']}: "
                   f"{', '.join(a['consequence']['blocking_findings']) or 'blocking finding recorded'}.{tail}", a)
    approved = [a for a in attempts if a["review"]["status"] == "approved"]
    if approved:
        a = approved[-1]
        st = a["promotion"]["status"]
        return out("approved_not_promoted", "Approved, not promoted", "review",
                   f"Impact report {a['impact_report_id']} was approved by {a['review'].get('reviewer')}; "
                   + ("promotion was requested but has no recorded result." if st == "requested"
                      else "promotion evidence is unavailable." if st == "unavailable"
                      else "no promotion was requested."), a)
    pending = [a for a in attempts if a["review"]["status"] == "none"]
    if pending:
        a = pending[-1]
        return out("awaiting_review", "Awaiting human review", "review",
                   f"Impact report {a['impact_report_id']} passed TwinGuard; no review decision is recorded.", a)
    auth = _authorization_summary(rec)
    if auth["denied"]:
        first = auth["denied_requests"][0]
        return out("unauthorized_request_denied", "Unauthorized request denied", "deny",
                   f"{auth['denied']} tool request(s) denied by IntentLock; first: {first['tool']} ({first['reason_code']}).")
    if attempts:
        return out("evidence_partial", "TwinGuard evidence only", "na",
                   "TwinGuard evaluated a patch, but review and promotion records are not part of this evidence.")
    status = rec.run.status if rec.run else None
    if status in ("failed", "stopped", "blocked"):
        return out(f"run_{status}", f"Run {status}", "stopped", rec.run.stop_reason or f"The run ended {status}.")
    if status == "completed":
        return out("run_completed", "Run completed", "ok", "The run completed without a patch or a denied request.")
    if status in ("active", "created"):
        return out("run_active", "Run in progress", "pending", "The run has not reached a terminal state.")
    return out("evidence_unavailable", "Evidence unavailable", "na", "No decision records are available for this run.")


def _claims(rec: Record, attempts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Integrity statements that are directly supported by a stored record. Nothing architectural is claimed."""
    claims = []
    for a in attempts:
        p = a.get("patch", {})
        if p.get("status") == "verified":
            claims.append({"claim": f"Stored patch for {a['impact_report_id']} hashes to the reported SHA-256 "
                                    f"{a['patch_sha256'][:16]}…", "basis": f"artifact {p['key']}"})
        elif p.get("status") == "hash_mismatch":
            claims.append({"claim": f"Stored patch for {a['impact_report_id']} does NOT match its reported SHA-256.",
                           "basis": f"artifact {p['key']}"})
        pr = a["promotion"]
        if pr["status"] == "promoted":
            claims.append({"claim": f"The promoter reported applying SHA-256 {pr['applied_patch_sha256'][:16]}… on base "
                                    f"commit {pr['base_commit'][:12]}, identical to the approved patch.",
                           "basis": f"promotion record (review {pr['review_id']})"})
        if a["consequence"]["status"] == "blocked" and pr["status"] == "none":
            claims.append({"claim": f"No promotion was requested for blocked report {a['impact_report_id']}.",
                           "basis": "review and promotion state in this evidence"})
    if rec.run and rec.run.status == "blocked" and rec.run.stop_reason:
        claims.append({"claim": f"IntentLock moved the run to BLOCKED: {rec.run.stop_reason}.", "basis": "run record"})
    return claims


# ----------------------------------------------------------------------------------------------- routes
def register(app, replay: Replay) -> None:
    @app.get("/api/replay/runs")
    def replay_runs(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return replay.catalog()

    @app.get("/api/replay/runs/{run_id}")
    def replay_run(run_id: str, response: Response,
                   source: str = Query("auto", pattern=r"^(auto|s3|local|control-plane)$")) -> dict:
        if not RUN_ID_RX.fullmatch(run_id):
            raise HTTPException(400, "INVALID_RUN_ID")
        view = replay.view(run_id, source)
        if view is None:
            raise HTTPException(404, "RUN_EVIDENCE_NOT_FOUND")
        response.headers["Cache-Control"] = "no-store"
        return view
