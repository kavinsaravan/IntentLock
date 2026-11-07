"""Redaction of tool arguments before they are stored or logged.

Never persist file contents, full patches, or secret-looking strings.
"""
import hashlib
import re
from typing import Any

_SECRET_RX = re.compile(r"(AKIA[0-9A-Z]{16}|[A-Za-z0-9+/=_\-]{32,})")
_SENSITIVE_KEYS = {"content", "contents", "patch", "token", "secret", "password",
                   "authorization", "api_key", "apikey", "credentials", "body"}


def scrub_text(text: str, max_len: int = 200) -> str:
    out = _SECRET_RX.sub("[REDACTED]", text)
    return out[:max_len] + ("…" if len(out) > max_len else "")


def redact_arguments(tool: str, args: Any) -> dict[str, Any]:
    """Return a safe, small description of tool arguments."""
    if not isinstance(args, dict):
        return {"_invalid": True}
    if tool in ("list_files", "read_file"):
        return {"path": scrub_text(str(args.get("path", "")), 300)}
    if tool == "search_code":
        paths = args.get("paths")
        return {
            "query": scrub_text(str(args.get("query", ""))),
            "paths": [scrub_text(str(p), 300) for p in paths[:16]] if isinstance(paths, list) else [],
        }
    if tool == "propose_patch":
        patch = args.get("patch")
        if not isinstance(patch, str):
            return {"_invalid": True}
        data = patch.encode("utf-8", "replace")
        return {"patch_sha256": hashlib.sha256(data).hexdigest(), "patch_bytes": len(data)}
    if tool == "run_approved_test":
        return {"test_id": scrub_text(str(args.get("test_id", "")), 64)}
    if tool == "request_permission":
        return {k: scrub_text(str(args.get(k, ""))) for k in ("tool", "resource", "reason")}
    if tool == "finish_task":
        return {"summary": scrub_text(str(args.get("summary", "")))}
    return {"keys": sorted(str(k) for k in args)[:16]}


def sanitize_details(obj: Any, depth: int = 0) -> Any:
    """Strip sensitive keys / secret-like strings from free-form event details."""
    if depth > 4:
        return "[TRUNCATED]"
    if isinstance(obj, dict):
        return {str(k)[:64]: ("[REDACTED]" if str(k).lower() in _SENSITIVE_KEYS
                              else sanitize_details(v, depth + 1))
                for k, v in list(obj.items())[:50]}
    if isinstance(obj, list):
        return [sanitize_details(v, depth + 1) for v in obj[:50]]
    if isinstance(obj, str):
        return scrub_text(obj, 500)
    return obj
