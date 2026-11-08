"""Deterministic authorization engine (PRD section 12).

`authorize()` is a pure function: it performs no I/O, never calls a model, and
fails closed. Callers load the run and contract and pass them in.

Evaluation order (PRD 12):
 1 request schema          2 load run+contract     3 exact contract version
 4 active run+contract     5 tool allowed          6 tool argument schema
 7-8 canonical paths       9 protected paths      10 allowed paths
11 approved test id       12 call/time limits     13-14 caller records + invokes
"""
import re
import time
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from .models import AuthorizationDecision, MissionContract, Run, ToolRequest
from .paths import (
    PathError,
    canonicalize_path,
    glob_base,
    has_glob,
    matches_allowed,
    matches_protected,
    validate_pattern,
)

# Always protected, regardless of what the contract says (a contract can only
# ADD protection, never remove it). Slash-less => matches at any depth.
BASELINE_PROTECTED = (".git", ".env*")

# Test infrastructure the agent may READ but never CHANGE (create/modify/delete/rename): TwinGuard judges a
# patch with these, so an agent-submitted patch must not be able to weaken them. Applies to propose_patch
# only; protected-path precedence and allowed-path rules still apply first. Case-insensitive; slash-less
# patterns match at any depth. Enforced again by TwinGuard (see docs/handoffs/person1-control-plane.md §9).
TRUSTED_TEST_PATTERNS = (
    "tests/**", "test/**", "__tests__/**", "**/tests/**", "**/test/**", "**/__tests__/**",
    "*.test.*", "*.spec.*", "jest.config.*", "vitest.config.*", ".mocharc*", "jest.setup.*", "setupTests.*",
)
# package.json keys whose changes can weaken the regression suite or run code at install time.
TRUSTED_SCRIPT_NAMES = frozenset({
    "scripts", "install", "preinstall", "postinstall", "prepare", "prepublish", "prepublishonly",
    "pretest", "test", "posttest", "jest", "mocha", "vitest", "ava", "nyc", "c8",
})
_PKG_RUNNERS = {"npm", "pnpm", "yarn"}
_COMMAND_ID_RX = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_KEY_RX = re.compile(r'"([^"]+)"\s*:')
_FILE_OPS = ("deleted file mode", "new file mode", "rename from", "rename to", "copy from", "copy to")

# No direct repository effect; allowed for any active run (still counted and
# argument-validated). Documented in docs/handoffs/person1-control-plane.md.
ALWAYS_ALLOWED_TOOLS = frozenset({"request_permission", "finish_task"})

# Tool names that imply network use. Network is never grantable (contract
# network_access is constant false), so these get NETWORK_NOT_ALLOWED.
NETWORK_TOOL_NAMES = frozenset(
    {"fetch", "http_request", "http_get", "curl", "wget", "web_search", "web_fetch",
     "browser", "open_url", "download"}
)

MAX_PATCH_BYTES = 1_000_000
MAX_QUERY_LEN = 256
MAX_TEXT_LEN = 2000

_MESSAGES = {
    "ALLOW": "Request is authorized by the mission contract.",
    "CONTRACT_NOT_FOUND": "No matching mission contract was found.",
    "CONTRACT_VERSION_MISMATCH": "The request does not reference the run's exact contract version.",
    "CONTRACT_NOT_ACTIVE": "The mission contract is not active.",
    "RUN_NOT_ACTIVE": "The run is not active.",
    "TOOL_NOT_ALLOWED": "The tool is not allowed by the mission contract.",
    "INVALID_ARGUMENTS": "The request or its arguments are invalid.",
    "PATH_NOT_ALLOWED": "The requested path is outside the paths allowed by the mission contract.",
    "PROTECTED_PATH": "The requested path is protected by the mission contract.",
    "TEST_NOT_APPROVED": "The test id is not in the contract's approved test catalogue.",
    "NETWORK_NOT_ALLOWED": "Network access is not allowed.",
    "TOOL_CALL_LIMIT_EXCEEDED": "The run exceeded its tool-call limit.",
    "POLICY_ENGINE_ERROR": "Internal policy error; request denied (fail closed).",
}


class _Deny(Exception):
    def __init__(self, code: str, evidence: dict[str, Any] | None = None, stop_run: bool = False):
        self.code, self.evidence, self.stop_run = code, evidence or {}, stop_run


def _decision(request_id: str, code: str, evidence: dict[str, Any], stop_run: bool,
              started: float) -> AuthorizationDecision:
    return AuthorizationDecision(
        request_id=request_id,
        decision="allow" if code == "ALLOW" else "deny",
        reason_code=code,  # type: ignore[arg-type]
        message=_MESSAGES[code],
        evidence=evidence,
        stop_run=stop_run,
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
    )


# --------------------------------------------------------------------------
# Patch path extraction
# --------------------------------------------------------------------------
def _strip_ab(p: str) -> str | None:
    """Return path without the mandatory a/ or b/ prefix; None for /dev/null."""
    p = p.split("\t")[0].rstrip("\r")
    if p == "/dev/null":
        return None
    if p.startswith('"'):
        raise PathError("quoted_path_unsupported")
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    raise PathError("missing_ab_prefix")


def extract_patch_paths(patch: str) -> list[str]:
    """Collect every file path named in a unified diff header.

    Deliberately conservative: any '--- '/'+++ ' line is treated as a header, so
    a hostile hunk cannot hide a path. Ambiguous input raises PathError.
    """
    raw: list[str] = []
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            toks = line[len("diff --git "):].split(" ")
            if len(toks) != 2:
                raise PathError("ambiguous_diff_header")
            raw.extend(filter(None, (_strip_ab(t) for t in toks)))
        elif line.startswith(("--- ", "+++ ")):
            p = _strip_ab(line[4:])
            if p:
                raw.append(p)
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            raw.append(line.split(" ", 2)[2].rstrip("\r"))
    if not raw:
        raise PathError("no_paths_in_patch")
    seen: list[str] = []
    for p in raw:
        c = canonicalize_path(p)
        if c not in seen:
            seen.append(c)
    return seen


# --------------------------------------------------------------------------
# Tool argument validation -> list of (kind, canonical path/base) to authorize
# --------------------------------------------------------------------------
def _require_keys(args: dict[str, Any], keys: set[str]) -> None:
    if set(args) != keys:
        raise _Deny("INVALID_ARGUMENTS", {"expected_keys": sorted(keys), "got_keys": sorted(args)})


def _require_str(args: dict[str, Any], key: str, max_len: int, *, allow_empty: bool = False) -> str:
    v = args.get(key)
    if not isinstance(v, str) or (not v and not allow_empty) or len(v) > max_len or "\x00" in v:
        raise _Deny("INVALID_ARGUMENTS", {"field": key})
    return v


def _canon(raw: object, field: str, *, allow_root: bool = False) -> str:
    try:
        return canonicalize_path(raw, allow_root=allow_root)
    except PathError as e:
        raise _Deny("INVALID_ARGUMENTS", {"field": field, "path_error": e.detail}) from e


def _validate_arguments(tool: str, args: dict[str, Any],
                        contract: MissionContract) -> tuple[list[str], list[str]]:
    """Validate tool args. Returns (paths to protect-check, paths that must be allowed)."""
    if tool in ("list_files", "read_file"):
        _require_keys(args, {"path"})
        c = _canon(args["path"], "path", allow_root=False)
        return [c], [c]
    if tool == "search_code":
        _require_keys(args, {"query", "paths"})
        _require_str(args, "query", MAX_QUERY_LEN)
        paths = args["paths"]
        if not isinstance(paths, list) or not (1 <= len(paths) <= 16):
            raise _Deny("INVALID_ARGUMENTS", {"field": "paths"})
        prot: list[str] = []
        need: list[str] = []
        for p in paths:
            try:
                pat = validate_pattern(p)
            except PathError as e:
                raise _Deny("INVALID_ARGUMENTS", {"field": "paths", "path_error": e.detail}) from e
            segs = pat.split("/")
            for i in range(1, len(segs) + 1):
                if has_glob(segs[i - 1]):
                    break
                prot.append("/".join(segs[:i]))
            need.append(glob_base(pat))  # "." (whole repo) never matches an allowed rule
        return prot, need
    if tool == "propose_patch":
        _require_keys(args, {"patch"})
        patch = args["patch"]
        if not isinstance(patch, str) or not patch.strip() or "\x00" in patch:
            raise _Deny("INVALID_ARGUMENTS", {"field": "patch"})
        if len(patch.encode("utf-8", "replace")) > MAX_PATCH_BYTES:
            raise _Deny("INVALID_ARGUMENTS", {"field": "patch", "error": "too_large"})
        try:
            pp = extract_patch_paths(patch)
            return pp, pp
        except PathError as e:
            raise _Deny("INVALID_ARGUMENTS", {"field": "patch", "path_error": e.detail}) from e
    if tool == "run_approved_test":
        _require_keys(args, {"test_id"})
        tid = _require_str(args, "test_id", 64)
        if tid not in contract.approved_tests:
            raise _Deny("TEST_NOT_APPROVED", {"test_id": tid[:64]})
        return [], []
    if tool == "run_approved_command":
        # The agent names a catalogue id; it can never supply argv, a shell string, or extra keys.
        _require_keys(args, {"command_id"})
        cid = _require_str(args, "command_id", 64)
        if not _COMMAND_ID_RX.fullmatch(cid):
            raise _Deny("INVALID_ARGUMENTS", {"field": "command_id"})
        if cid not in contract.approved_tests:
            raise _Deny("TEST_NOT_APPROVED", {"command_id": cid})
        return [], []
    if tool == "request_permission":
        _require_keys(args, {"tool", "resource", "reason"})
        for k in ("tool", "resource", "reason"):
            _require_str(args, k, MAX_TEXT_LEN)
        return [], []
    if tool == "finish_task":
        _require_keys(args, {"summary"})
        _require_str(args, "summary", MAX_TEXT_LEN, allow_empty=True)
        return [], []
    raise _Deny("TOOL_NOT_ALLOWED", {"tool": tool[:64]})  # unreachable for known tools


def _check_paths(prot_paths: list[str], need_paths: list[str], contract: MissionContract) -> None:
    protected = list(BASELINE_PROTECTED) + list(contract.protected_paths)
    # Protected paths take precedence over allowed paths: check them all first.
    for p in prot_paths:
        for rule in protected:
            if matches_protected(rule, p):
                raise _Deny("PROTECTED_PATH", {"requested_path": p, "matched_rule": rule}, stop_run=True)
    for p in need_paths:
        if not any(matches_allowed(rule, p) for rule in contract.allowed_paths):
            raise _Deny("PATH_NOT_ALLOWED", {"requested_path": p})


def _approved_test_footprint(contract: MissionContract) -> tuple[set[str], set[str]]:
    """(package.json script names, repo-relative file paths) referenced by approved test argv."""
    scripts: set[str] = set()
    files: set[str] = set()
    for t in contract.approved_tests.values():
        argv = t.argv
        if argv and argv[0] in _PKG_RUNNERS:
            rest = argv[1:]
            if rest[:1] in (["run"], ["run-script"]) and len(rest) > 1:
                scripts.add(rest[1].lower())
            elif rest[:1] == ["test"] or rest[:1] == ["t"]:
                scripts.add("test")
            elif rest and not rest[0].startswith("-"):
                scripts.add(rest[0].lower())      # yarn <script>
        for a in argv[1:] if argv else []:
            if not a.startswith("-") and ("/" in a or a.endswith((".js", ".mjs", ".cjs", ".ts", ".sh", ".py", ".json"))):
                try:
                    files.add(canonicalize_path(a))
                except PathError:
                    pass
    scripts |= {f"pre{n}" for n in list(scripts)} | {f"post{n}" for n in list(scripts)}
    return scripts, files


def _check_trusted_tests(patch: str, paths: list[str], contract: MissionContract) -> None:
    """A patch may not create/modify/delete/rename test infrastructure (see TRUSTED_TEST_PATTERNS)."""
    scripts, files = _approved_test_footprint(contract)
    for p in paths:
        for rule in TRUSTED_TEST_PATTERNS:
            if matches_protected(rule, p):
                raise _Deny("PATH_NOT_ALLOWED", {"requested_path": p, "matched_rule": rule, "rule": "TRUSTED_TEST_READ_ONLY"})
        if p in files:
            raise _Deny("PATH_NOT_ALLOWED", {"requested_path": p, "matched_rule": "approved_test_argv",
                                              "rule": "TRUSTED_TEST_READ_ONLY"})
    if any(p == "package.json" or p.endswith("/package.json") for p in paths):
        lines = patch.split("\n")
        if any(l.startswith(_FILE_OPS) for l in lines):
            raise _Deny("PATH_NOT_ALLOWED", {"requested_path": "package.json", "matched_rule": "package.json file operation",
                                              "rule": "TRUSTED_TEST_READ_ONLY"})
        for l in lines:
            if l[:1] in ("+", "-") and not l.startswith(("+++", "---")):
                for key in _KEY_RX.findall(l):
                    k = key.lower()
                    if k in TRUSTED_SCRIPT_NAMES or k in scripts or k.startswith("test"):
                        raise _Deny("PATH_NOT_ALLOWED", {"requested_path": "package.json", "matched_rule": f"script:{key[:40]}",
                                                          "rule": "TRUSTED_TEST_READ_ONLY"})


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
def _evaluate(raw_request: Any, run: Run | None, contract: MissionContract | None,
              prior_tool_calls: int, now: datetime) -> tuple[str, dict[str, Any], bool]:
    # 1. request schema
    try:
        req = ToolRequest.model_validate(raw_request)
    except ValidationError as e:
        raise _Deny("INVALID_ARGUMENTS", {"error": "request_schema", "fields": sorted(
            {str(err["loc"][0]) for err in e.errors() if err["loc"]})}) from e

    # 2. load run + contract (caller loaded them; None means missing)
    if run is None:
        raise _Deny("RUN_NOT_ACTIVE", {"run_found": False})
    if run.run_id != req.run_id:
        raise _Deny("RUN_NOT_ACTIVE", {"run_found": False})
    if contract is None:
        raise _Deny("CONTRACT_NOT_FOUND", {"contract_id": req.contract_id})

    # 3. exact contract version
    if req.contract_id != run.contract_id:
        raise _Deny("CONTRACT_NOT_FOUND", {"contract_id": req.contract_id})
    if (req.contract_version != run.contract_version
            or contract.contract_id != run.contract_id
            or contract.version != run.contract_version):
        raise _Deny("CONTRACT_VERSION_MISMATCH", {
            "requested_version": req.contract_version, "run_version": run.contract_version})

    # 4. active state
    if contract.status != "active":
        raise _Deny("CONTRACT_NOT_ACTIVE", {"contract_status": contract.status})
    if run.status != "active":
        raise _Deny("RUN_NOT_ACTIVE", {"run_status": run.status})

    # 5. tool allowlist
    if req.tool in NETWORK_TOOL_NAMES:
        raise _Deny("NETWORK_NOT_ALLOWED", {"tool": req.tool})
    if req.tool not in contract.allowed_tools and req.tool not in ALWAYS_ALLOWED_TOOLS:
        raise _Deny("TOOL_NOT_ALLOWED", {"tool": req.tool[:64]})

    # 6-8, 11. argument schema, canonical paths, test ids
    prot_paths, need_paths = _validate_arguments(req.tool, req.arguments, contract)

    # 9-10. protected, then allowed
    _check_paths(prot_paths, need_paths, contract)
    if req.tool == "propose_patch":  # approved tests must come from a trusted baseline, never from the patch
        _check_trusted_tests(req.arguments["patch"], need_paths, contract)

    # 12. limits
    if prior_tool_calls >= contract.limits.max_tool_calls:
        raise _Deny("TOOL_CALL_LIMIT_EXCEEDED", {
            "max_tool_calls": contract.limits.max_tool_calls, "prior_tool_calls": prior_tool_calls})
    if run.started_at is not None:
        elapsed = (now - run.started_at).total_seconds()
        if elapsed >= contract.limits.run_timeout_seconds:
            # No dedicated reason code in schema 0.1.0; see handoff "proposed change".
            raise _Deny("RUN_NOT_ACTIVE", {
                "cause": "run_timeout", "run_timeout_seconds": contract.limits.run_timeout_seconds},
                stop_run=True)

    evidence: dict[str, Any] = {}
    if need_paths:
        evidence["checked_paths"] = need_paths[:32]
    return "ALLOW", evidence, False


def authorize(raw_request: Any, run: Run | None, contract: MissionContract | None,
              prior_tool_calls: int, now: datetime | None = None) -> AuthorizationDecision:
    """Evaluate one tool request. Never raises; any internal error => deny.

    prior_tool_calls: number of earlier requests recorded for this run
    (allowed or denied), excluding the current one.
    """
    started = time.perf_counter()
    request_id = "unknown"
    if isinstance(raw_request, dict) and isinstance(raw_request.get("request_id"), str):
        request_id = raw_request["request_id"][:128] or "unknown"
    try:
        code, evidence, stop = _evaluate(
            raw_request, run, contract, prior_tool_calls, now or datetime.now(timezone.utc))
    except _Deny as d:
        code, evidence, stop = d.code, d.evidence, d.stop_run
    except Exception:  # fail closed on ANY unexpected error
        code, evidence, stop = "POLICY_ENGINE_ERROR", {}, False
    return _decision(request_id, code, evidence, stop, started)
