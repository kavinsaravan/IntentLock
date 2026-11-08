"""Path canonicalization and glob matching for the deterministic policy.

All functions are pure. Anything suspicious raises PathError (fail closed).
"""
import re
from urllib.parse import unquote

MAX_PATH_LEN = 1024
_GLOB_CHARS = set("*?[")


class PathError(ValueError):
    """Raised when a path or pattern is invalid. `detail` is a stable short code."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _check_traversal_segments(path: str) -> None:
    for seg in path.split("/"):
        if seg == "..":
            raise PathError("traversal")


def _bad_chars(path: str) -> str | None:
    if "\x00" in path:
        return "nul_byte"
    if "\\" in path:
        return "backslash"
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        return "control_character"
    return None


def _check_raw(path: object) -> str:
    if not isinstance(path, str):
        raise PathError("not_a_string")
    if not path:
        raise PathError("empty")
    if len(path) > MAX_PATH_LEN:
        raise PathError("too_long")
    bad = _bad_chars(path)
    if bad:
        raise PathError(bad)
    if path.startswith("/") or path.startswith("~") or re.match(r"^[A-Za-z]:", path):
        raise PathError("absolute")
    # URL-encoded tricks: decode to a fixed point and re-run the checks.
    decoded = path
    for _ in range(4):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    if decoded != path:
        bad = _bad_chars(decoded)
        if bad:
            raise PathError("encoded_" + bad)
        if decoded.startswith("/") or decoded.startswith("~"):
            raise PathError("encoded_absolute")
        _check_traversal_segments(decoded)
    _check_traversal_segments(path)
    return path


def canonicalize_path(raw: object, *, allow_root: bool = False) -> str:
    """Return a repo-relative canonical path ('a/b/c'), or '.' for the root.

    Rejects absolute paths, '..' segments, NUL/control chars, backslashes and
    URL-encoded variants of those.
    """
    path = _check_raw(raw)
    segs = [s for s in path.split("/") if s not in ("", ".")]
    if not segs:
        if allow_root:
            return "."
        raise PathError("root_not_allowed")
    return "/".join(segs)


def validate_pattern(raw: object) -> str:
    """Validate a contract glob pattern (same rules as paths, wildcards allowed)."""
    path = _check_raw(raw)
    segs = [s for s in path.split("/") if s not in ("", ".")]
    if not segs:
        raise PathError("empty")
    return "/".join(segs)


def glob_base(pattern: str) -> str:
    """Longest wildcard-free directory prefix of a canonical glob ('.' if none)."""
    base = []
    for seg in pattern.split("/"):
        if any(c in _GLOB_CHARS for c in seg):
            break
        base.append(seg)
    return "/".join(base) if base else "."


def has_glob(pattern: str) -> bool:
    return any(c in _GLOB_CHARS for c in pattern)


def _seg_regex(seg: str) -> str:
    out = []
    for ch in seg:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _glob_regex(pattern: str) -> re.Pattern:
    segs = pattern.split("/")
    parts = []
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        if seg == "**":
            parts.append(".*" if last else "(?:.*/)?")
        else:
            parts.append(_seg_regex(seg) + ("" if last else "/"))
    return re.compile("^" + "".join(parts) + "$")


def matches_anchored(pattern: str, path: str, *, case_insensitive: bool = False) -> bool:
    """Root-anchored glob match. 'dir/**' also matches 'dir' itself."""
    if path == ".":
        return False
    pattern = pattern.lstrip("./") if pattern.startswith("./") else pattern
    flags = re.IGNORECASE if case_insensitive else 0
    rx = _glob_regex(pattern)
    if re.compile(rx.pattern, flags).match(path):
        return True
    if pattern.endswith("/**"):
        return bool(re.compile(_glob_regex(pattern[:-3]).pattern, flags).match(path))
    return False


def matches_allowed(pattern: str, path: str) -> bool:
    """Allowed patterns are ALWAYS root-anchored and case-sensitive."""
    return matches_anchored(pattern, path)


def matches_protected(pattern: str, path: str) -> bool:
    """Protected patterns match case-insensitively (safe direction).

    A pattern with no '/' (e.g. '.env*') matches any path segment at any depth,
    so it also protects everything beneath a matching directory.
    """
    if path == ".":
        return False
    if "/" not in pattern:
        rx = re.compile("^" + _seg_regex(pattern) + "$", re.IGNORECASE)
        return any(rx.match(seg) for seg in path.split("/"))
    return matches_anchored(pattern, path, case_insensitive=True)
