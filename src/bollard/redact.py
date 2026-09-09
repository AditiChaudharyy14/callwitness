"""Secret redaction for the recorder write path. Python 3.8+, standard library only.

Design contract, in priority order:

1. NEVER RAISE. This runs inside the recorder, which runs beside a live protocol
   stream. Every public function is total: any input returns a value. A redaction
   bug must not become a traffic bug.
2. Redact BEFORE the value reaches storage. A scrubber that runs on read leaves
   plaintext on disk; that is the failure this module exists to prevent.
3. Preserve the signals. Destinations are the point of the product, so hosts,
   emails and paths survive redaction -- only credentials inside them are removed.
   True byte length is the caller's responsibility to record before redaction.

Placeholders are `<redacted:REASON>` so a reader can tell why a value went, and
so downstream analysis can count redactions by cause without seeing the values.
"""
import math
import re
from typing import Any, Dict, Tuple

__all__ = ["redact_value", "redact_structure", "RedactionStats", "DEFAULT_KEY_NAMES"]

# --- Key names that redact regardless of value shape -------------------------
# Cheapest, highest-recall signal available: the tool's own parameter name.
DEFAULT_KEY_NAMES = frozenset({
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "access_token", "refresh_token", "id_token", "auth", "authorization",
    "credential", "credentials", "private_key", "privatekey", "client_secret",
    "session_key", "encryption_key", "signing_key", "bearer", "cookie",
    "set_cookie", "x_api_key", "aws_secret_access_key", "connection_string",
    "dsn", "passphrase", "otp", "mfa_code", "pin",
})

# --- Known credential shapes -------------------------------------------------
# Ordered most-specific first; the first match wins and names the reason.
_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9\-_]{20,}")),
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}")),
    ("slack_token", re.compile(r"\bxox[baprse]-[A-Za-z0-9\-]{10,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[A-Za-z0-9\-_]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9\-_]+\.eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]*")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{16,}=*", re.IGNORECASE)),
    ("basic_auth", re.compile(r"\bBasic\s+[A-Za-z0-9+/]{16,}=*", re.IGNORECASE)),
]

# Credentials embedded in a URL's userinfo: scheme://user:pass@host
_URL_USERINFO = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@")

# Credential-bearing query parameters. The host survives; the value does not.
_QUERY_SECRET = re.compile(
    r"([?&](?:token|api_key|apikey|access_token|key|sig|signature|password|auth)=)"
    r"([^&\s\"']+)",
    re.IGNORECASE,
)

# UUIDs are identifiers, not secrets, and they clear the entropy bar. Exempt them.
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# A token-shaped run: the only thing the entropy heuristic is allowed to consider.
_TOKENISH = re.compile(r"\b[A-Za-z0-9+/=_\-]{24,}\b")

_ENTROPY_MIN_LEN = 24
_ENTROPY_THRESHOLD = 3.6  # bits/char; English prose sits well below this


class RedactionStats:
    """Counts by reason. Never contains a redacted value."""

    __slots__ = ("counts",)

    def __init__(self) -> None:
        self.counts: Dict[str, int] = {}

    def hit(self, reason: str) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def as_dict(self) -> Dict[str, Any]:
        return {"total": self.total, "by_reason": dict(self.counts)}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "RedactionStats({!r})".format(self.counts)


def _shannon_entropy(s: str) -> float:
    """Bits per character. Empty string is 0.0."""
    if not s:
        return 0.0
    freq: Dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def _redact_high_entropy(text: str, stats: RedactionStats) -> str:
    """Replace token-shaped, high-entropy runs. UUIDs are exempt."""
    def repl(m: "re.Match") -> str:
        tok = m.group(0)
        if _UUID.fullmatch(tok):
            return tok
        if len(tok) < _ENTROPY_MIN_LEN:
            return tok
        if _shannon_entropy(tok) < _ENTROPY_THRESHOLD:
            return tok
        stats.hit("high_entropy")
        return "<redacted:high_entropy:{}>".format(len(tok))

    return _TOKENISH.sub(repl, text)


def redact_value(value: Any, key_name: str = "", stats: "RedactionStats" = None,
                 entropy: bool = True) -> Tuple[Any, bool]:
    """Redact one value. Returns (value_or_placeholder, was_redacted).

    `key_name` is the parameter name this value was found under; a sensitive
    name redacts the whole value regardless of its shape. Non-string values are
    returned unchanged unless the key name triggers.

    Total function: any exception is swallowed and the value is redacted
    wholesale, because failing closed on a possible secret is the safe direction
    and failing loudly is never an option on this path.
    """
    if stats is None:
        stats = RedactionStats()
    try:
        if key_name and key_name.strip().lower().replace("-", "_") in DEFAULT_KEY_NAMES:
            stats.hit("key_name")
            return "<redacted:key_name>", True

        if not isinstance(value, str) or not value:
            return value, False

        original = value

        for reason, pattern in _PATTERNS:
            if pattern.search(value):
                value = pattern.sub("<redacted:{}>".format(reason), value)
                stats.hit(reason)

        # Credentials inside URLs -- keep scheme, user and host, drop the password.
        if "://" in value:
            def _userinfo(m: "re.Match") -> str:
                stats.hit("url_password")
                return "{}{}:<redacted:url_password>@".format(m.group(1), m.group(2))

            value = _URL_USERINFO.sub(_userinfo, value)

            def _query(m: "re.Match") -> str:
                stats.hit("url_query_secret")
                return "{}<redacted:url_query_secret>".format(m.group(1))

            value = _QUERY_SECRET.sub(_query, value)

        if entropy:
            value = _redact_high_entropy(value, stats)

        return value, value != original
    except Exception:  # noqa: BLE001 - contract: never raise on the write path
        try:
            stats.hit("redactor_error")
        except Exception:  # noqa: BLE001
            pass
        return "<redacted:redactor_error>", True


def redact_structure(obj: Any, entropy: bool = True, _depth: int = 0,
                     stats: "RedactionStats" = None) -> Tuple[Any, "RedactionStats"]:
    """Walk a decoded JSON structure, redacting in place-equivalent form.

    Returns (redacted_copy, stats). The caller records the TRUE byte length of
    the original before calling this -- redaction changes length, and the volume
    signal is load-bearing for the product.
    """
    if stats is None:
        stats = RedactionStats()
    try:
        if _depth > 64:  # cycle / bomb guard
            return "<redacted:max_depth>", stats
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                key = k if isinstance(k, str) else str(k)
                if isinstance(v, (dict, list)):
                    if key.strip().lower().replace("-", "_") in DEFAULT_KEY_NAMES:
                        stats.hit("key_name")
                        out[k] = "<redacted:key_name>"
                    else:
                        out[k], _ = redact_structure(v, entropy, _depth + 1, stats)
                else:
                    out[k], _ = redact_value(v, key, stats, entropy)
            return out, stats
        if isinstance(obj, list):
            return [redact_structure(v, entropy, _depth + 1, stats)[0] for v in obj], stats
        redacted, _ = redact_value(obj, "", stats, entropy)
        return redacted, stats
    except Exception:  # noqa: BLE001 - contract: never raise
        try:
            stats.hit("redactor_error")
        except Exception:  # noqa: BLE001
            pass
        return "<redacted:redactor_error>", stats
