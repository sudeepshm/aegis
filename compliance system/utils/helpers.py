"""
utils/helpers.py
────────────────────────────────────────────────────────────────────────────
Cross-cutting utility functions for the Compliance System.

Sections
────────
  1. Hashing        – SHA-256 for files, byte streams, and JSON payloads
  2. Timestamps     – ISO 8601 helpers anchored to Asia/Kolkata (IST, UTC+05:30)
  3. Retry          – exponential-backoff decorator for external API calls
  4. Data Masking   – PII / sensitive-field scrubbing for logs & telemetry
  5. Schema         – JSON payload loading and schema validation

Dependencies
────────────
  stdlib only (Python ≥ 3.9):
    hashlib, json, re, time, os, copy, functools, io, logging,
    pathlib, datetime, zoneinfo, typing, collections.abc
"""

from __future__ import annotations

import copy
import functools
import hashlib
import io
import json
import logging
import os
import re
import time
from collections.abc import Generator, Iterable
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

F = TypeVar("F", bound=Callable[..., Any])
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
_IST_OFFSET = timedelta(hours=5, minutes=30)
_CHUNK_SIZE  = 65_536


# ══════════════════════════════════════════════════════════════════════════════
# 1. HASHING
# ══════════════════════════════════════════════════════════════════════════════

def hash_bytes(data: bytes, algorithm: str = "sha256") -> str:
    """Compute a hex digest of raw bytes."""
    h = hashlib.new(algorithm)
    h.update(data)
    return h.hexdigest()


def hash_string(text: str, encoding: str = "utf-8", algorithm: str = "sha256") -> str:
    """Compute a hex digest of an encoded string."""
    return hash_bytes(text.encode(encoding), algorithm=algorithm)


def hash_file(path: str | Path, algorithm: str = "sha256") -> str:
    """Stream-hash a file in 64 KB chunks without loading it into memory.

    Raises
    ------
    FileNotFoundError  If *path* does not exist.
    PermissionError    If the process lacks read access.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    h = hashlib.new(algorithm)
    with path.open("rb") as fh:
        for chunk in _read_chunks(fh, _CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def hash_payload(payload: dict[str, Any] | list[Any], algorithm: str = "sha256") -> str:
    """Deterministically hash a JSON-serialisable payload (keys are sorted).

    Raises
    ------
    TypeError  If *payload* contains non-serialisable values.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hash_string(canonical, algorithm=algorithm)


def verify_file_integrity(path: str | Path, expected_hash: str, algorithm: str = "sha256") -> bool:
    """Return True if the file's digest matches *expected_hash* (case-insensitive)."""
    actual = hash_file(path, algorithm=algorithm)
    match  = actual == expected_hash.lower()
    if not match:
        logger.warning(
            "Integrity check FAILED for '%s' | expected=%s | actual=%s",
            path, expected_hash.lower(), actual,
        )
    return match


def _read_chunks(fh: io.BufferedReader, size: int) -> Generator[bytes, None, None]:
    while True:
        chunk = fh.read(size)
        if not chunk:
            break
        yield chunk


# ══════════════════════════════════════════════════════════════════════════════
# 2. TIMESTAMPS  (IST / Asia/Kolkata)
# ══════════════════════════════════════════════════════════════════════════════

def now_ist() -> datetime:
    """Return the current time as a timezone-aware datetime in IST (UTC+05:30)."""
    return datetime.now(tz=IST)


def utc_to_ist(dt: datetime) -> datetime:
    """Convert any timezone-aware *dt* to IST.

    Raises
    ------
    ValueError  If *dt* is timezone-naïve.
    """
    if dt.tzinfo is None:
        raise ValueError(
            "utc_to_ist() requires a timezone-aware datetime. "
            "Pass datetime.now(tz=timezone.utc) or similar."
        )
    return dt.astimezone(IST)


def format_iso8601(dt: datetime | None = None, *, include_microseconds: bool = False) -> str:
    """Format a datetime as ISO 8601 in IST.

    If *dt* is None, the current IST time is used.
    Naïve datetimes are assumed to be in IST.

    Returns strings like ``"2024-11-18T14:35:22+05:30"``.
    """
    if dt is None:
        dt = now_ist()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    else:
        dt = dt.astimezone(IST)

    fmt = "%Y-%m-%dT%H:%M:%S.%f%z" if include_microseconds else "%Y-%m-%dT%H:%M:%S%z"
    return _coerce_offset_colon(dt.strftime(fmt))


def parse_iso8601(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp string, returning an IST-aware datetime.

    Handles ``+0530``, ``+05:30``, and ``Z`` suffixes.

    Raises
    ------
    ValueError  If *ts* cannot be parsed.
    """
    normalised = ts.strip().replace("Z", "+00:00")
    normalised = re.sub(r"([+-])(\d{2}):(\d{2})$", r"\1\2\3", normalised)
    try:
        dt = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise ValueError(f"Cannot parse ISO 8601 timestamp '{ts}': {exc}") from exc
    return dt.astimezone(IST)


def elapsed_since(start: datetime) -> float:
    """Return wall-clock seconds elapsed since *start* (naïve → treated as IST)."""
    now = now_ist()
    if start.tzinfo is None:
        start = start.replace(tzinfo=IST)
    return (now - start).total_seconds()


def _coerce_offset_colon(ts: str) -> str:
    """Rewrite ``+0530`` → ``+05:30`` to comply with ISO 8601."""
    return re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", ts)


# ══════════════════════════════════════════════════════════════════════════════
# 3. RETRY DECORATOR  (exponential back-off)
# ══════════════════════════════════════════════════════════════════════════════

_ALWAYS_TRANSIENT: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    OSError,
)


def retry(
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: float = 0.1,
    retriable_exceptions: tuple[type[BaseException], ...] = _ALWAYS_TRANSIENT,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> Callable[[F], F]:
    """Exponential back-off retry decorator for transient external API failures.

    The wait between attempt k and k+1 is::

        delay = min(base_delay × backoff_factor^(k-1), max_delay)
              ± jitter fraction

    Parameters
    ----------
    max_attempts:           Total attempts including the first (default 3).
    base_delay:             Initial wait in seconds (default 1 s).
    max_delay:              Ceiling for any single sleep (default 60 s).
    backoff_factor:         Multiplier per failure (default 2×).
    jitter:                 Fractional noise to avoid thundering herd (default ±10 %).
    retriable_exceptions:   Exception types that trigger a retry.
    on_retry:               Optional ``(attempt, exc, wait_secs)`` callback.

    Example
    -------
    >>> @retry(max_attempts=5, base_delay=0.5, retriable_exceptions=(IOError,))
    ... def call_azure_openai(prompt: str) -> str: ...
    """
    all_retriable = tuple(set(retriable_exceptions) | set(_ALWAYS_TRANSIENT))

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except all_retriable as exc:  # type: ignore[misc]
                    last_exc = exc
                    if attempt == max_attempts:
                        logger.error(
                            "[retry] '%s' exhausted %d/%d attempts. Last error: %s",
                            func.__qualname__, attempt, max_attempts, exc,
                        )
                        raise
                    raw_delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)
                    noise     = raw_delay * jitter * (2 * _uniform_01() - 1)
                    sleep_for = max(0.0, raw_delay + noise)
                    if on_retry is not None:
                        on_retry(attempt, exc, sleep_for)
                    else:
                        logger.warning(
                            "[retry] '%s' attempt %d/%d failed (%s). Retrying in %.2fs…",
                            func.__qualname__, attempt, max_attempts, exc, sleep_for,
                        )
                    time.sleep(sleep_for)
            raise RuntimeError(f"Retry loop exhausted for '{func.__qualname__}'") from last_exc
        return wrapper  # type: ignore[return-value]
    return decorator


def retry_async(
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: float = 0.1,
    retriable_exceptions: tuple[type[BaseException], ...] = _ALWAYS_TRANSIENT,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Async-aware exponential backoff retry decorator for coroutines.

    Works like `retry` but expects the decorated function to be an
    `async def` coroutine and uses `asyncio.sleep` between retries.
    """
    all_retriable = tuple(set(retriable_exceptions) | set(_ALWAYS_TRANSIENT))

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if not asyncio.iscoroutinefunction(func):
            raise TypeError("retry_async can only decorate async functions")

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except all_retriable as exc:  # type: ignore[misc]
                    last_exc = exc
                    if attempt == max_attempts:
                        logger.error(
                            "[retry_async] '%s' exhausted %d/%d attempts. Last error: %s",
                            func.__qualname__, attempt, max_attempts, exc,
                        )
                        raise
                    raw_delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)
                    noise     = raw_delay * jitter * (2 * _uniform_01() - 1)
                    sleep_for = max(0.0, raw_delay + noise)
                    if on_retry is not None:
                        on_retry(attempt, exc, sleep_for)
                    else:
                        logger.warning(
                            "[retry_async] '%s' attempt %d/%d failed (%s). Retrying in %.2fs…",
                            func.__qualname__, attempt, max_attempts, exc, sleep_for,
                        )
                    await asyncio.sleep(sleep_for)
            raise RuntimeError(f"Retry loop exhausted for '{func.__qualname__}'") from last_exc

        return wrapper

    return decorator


def _uniform_01() -> float:
    return int.from_bytes(os.urandom(4), "big") / 0xFFFF_FFFF


# ══════════════════════════════════════════════════════════════════════════════
# 4. DATA MASKING  (PII / sensitive-field scrubbing)
# ══════════════════════════════════════════════════════════════════════════════

_SENSITIVE_FIELDS: frozenset[str] = frozenset({
    "pan", "aadhaar", "aadhar", "passport", "voter_id", "driving_license",
    "ssn", "tax_id", "national_id", "cin", "gstin",
    "email", "phone", "mobile", "fax", "address", "pincode", "zip",
    "account_number", "account_no", "ifsc", "swift", "iban",
    "card_number", "cvv", "credit_card", "debit_card", "upi_id", "vpa", "bank_account",
    "password", "passwd", "secret", "token", "api_key", "access_key",
    "private_key", "client_secret", "otp", "pin", "auth_token",
    "refresh_token", "bearer",
    "diagnosis", "medical_record", "prescription", "biometric",
    "dob", "date_of_birth", "birth_date", "gender", "religion", "caste",
})

_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.IGNORECASE), "[EMAIL]"),
    (re.compile(r"(?<!\d)(?:\+91[\s\-]?|0)?[6-9]\d{9}(?!\d)"), "[PHONE]"),
    (re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"), "[AADHAAR]"),
    (re.compile(r"\b(?:\d[\s\-]?){13,19}\b"), "[CARD]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    (re.compile(r"eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+"), "[JWT]"),
    (re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"), "[SECRET]"),
]

_MASK = "***REDACTED***"


def mask_dict(
    data: dict[str, Any],
    *,
    extra_fields: Iterable[str] = (),
    deep: bool = True,
) -> dict[str, Any]:
    """Return a deep copy of *data* with sensitive fields replaced by ``***REDACTED***``.

    Parameters
    ----------
    data:           Input mapping (nested when *deep* is True).
    extra_fields:   Extra field names to redact (case-insensitive).
    deep:           When True (default), recurse into nested dicts and lists.
    """
    sensitive = _SENSITIVE_FIELDS | frozenset(f.lower() for f in extra_fields)
    return _mask_recursive(copy.deepcopy(data), sensitive, deep=deep)


def mask_string(text: str) -> str:
    """Replace PII patterns in *text* with labelled tokens such as ``[EMAIL]``.

    Example
    -------
    >>> mask_string("Contact alice@example.com or call 9876543210")
    'Contact [EMAIL] or call [PHONE]'
    """
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def mask_payload_for_logging(
    payload: dict[str, Any],
    *,
    extra_fields: Iterable[str] = (),
) -> dict[str, Any]:
    """Mask a dict at both field-key level and inline string value level.

    Safe to pass to ``json.dumps()`` before writing to logs or monitoring.
    """
    masked = mask_dict(payload, extra_fields=extra_fields)
    return _scrub_string_values(masked)


def _mask_recursive(obj: Any, sensitive: frozenset[str], *, deep: bool) -> Any:
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            if k.lower() in sensitive:
                result[k] = _MASK
            elif deep:
                result[k] = _mask_recursive(v, sensitive, deep=deep)
            else:
                result[k] = v
        return result
    if deep and isinstance(obj, list):
        return [_mask_recursive(item, sensitive, deep=deep) for item in obj]
    return obj


def _scrub_string_values(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _scrub_string_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_string_values(i) for i in obj]
    if isinstance(obj, str):
        return mask_string(obj)
    return obj


# ══════════════════════════════════════════════════════════════════════════════
# 5. SCHEMA VALIDATION  (JSON payload loading & validation)
# ══════════════════════════════════════════════════════════════════════════════

class SchemaValidationError(ValueError):
    """Raised when a payload does not conform to its schema.

    Attributes
    ----------
    errors:  List of human-readable validation error messages.
    payload: The original payload that failed validation.
    """

    def __init__(self, errors: list[str], payload: Any) -> None:
        self.errors  = errors
        self.payload = payload
        summary = "; ".join(errors[:3])
        if len(errors) > 3:
            summary += f" … (+{len(errors) - 3} more)"
        super().__init__(f"{len(errors)} schema violation(s): {summary}")


def load_json_file(path: str | Path, *, encoding: str = "utf-8") -> Any:
    """Load and parse a JSON file from disk.

    Raises
    ------
    FileNotFoundError    If *path* does not exist.
    json.JSONDecodeError If the file is not valid JSON.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    with path.open("r", encoding=encoding) as fh:
        return json.load(fh)


def load_json_string(raw: str) -> Any:
    """Parse a JSON string.

    Raises
    ------
    json.JSONDecodeError  If *raw* is not valid JSON.
    """
    return json.loads(raw)


def validate_payload(
    payload: dict[str, Any] | list[Any],
    schema: dict[str, Any],
    *,
    raise_on_error: bool = True,
    allow_extra_fields: bool = True,
) -> list[str]:
    """Validate *payload* against a JSON Schema-inspired *schema* dict.

    Supported keywords: ``type``, ``required``, ``properties``, ``items``,
    ``minLength``, ``maxLength``, ``minimum``, ``maximum``, ``enum``,
    ``additionalProperties``, ``minItems``, ``maxItems``, ``pattern``.

    Parameters
    ----------
    payload:            Dict or list to validate.
    schema:             Schema dict.
    raise_on_error:     If True (default), raise SchemaValidationError.
    allow_extra_fields: When False, undeclared object properties are errors.

    Returns
    -------
    List of error strings.  Empty list means payload is valid.

    Example
    -------
    >>> schema = {"type": "object", "required": ["doc_id"],
    ...           "properties": {"doc_id": {"type": "string"}}}
    >>> validate_payload({"x": 1}, schema, raise_on_error=False)
    ["$.doc_id: 'doc_id' is a required property"]
    """
    effective_schema = copy.deepcopy(schema)
    if not allow_extra_fields:
        _inject_no_additional(effective_schema)
    errors = _validate(payload, effective_schema, path="$")
    if errors and raise_on_error:
        raise SchemaValidationError(errors, payload)
    return errors


def load_and_validate(
    path: str | Path,
    schema: dict[str, Any],
    *,
    raise_on_error: bool = True,
    allow_extra_fields: bool = True,
) -> Any:
    """Load a JSON file and validate it against *schema* in one call.

    Raises
    ------
    FileNotFoundError       If the file does not exist.
    json.JSONDecodeError    If the file is not valid JSON.
    SchemaValidationError   If the payload fails schema validation.
    """
    payload = load_json_file(path)
    validate_payload(payload, schema, raise_on_error=raise_on_error,
                     allow_extra_fields=allow_extra_fields)
    logger.debug("Payload at '%s' passed schema validation.", path)
    return payload


# ── internal schema engine ────────────────────────────────────────────────────

_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string":  str,
    "integer": int,
    "number":  (int, float),
    "boolean": bool,
    "null":    type(None),
    "object":  dict,
    "array":   list,
}


def _validate(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []

    expected_type = schema.get("type")
    if expected_type:
        py_type = _TYPE_MAP.get(expected_type)
        if py_type and not isinstance(value, py_type):
            errors.append(
                f"{path}: expected type '{expected_type}', got '{type(value).__name__}'"
            )
            return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value {value!r} not in allowed enum {schema['enum']!r}")

    if isinstance(value, dict):
        for field in schema.get("required", []):
            if field not in value:
                errors.append(f"{path}.{field}: '{field}' is a required property")
        for prop, sub_schema in schema.get("properties", {}).items():
            if prop in value:
                errors.extend(_validate(value[prop], sub_schema, f"{path}.{prop}"))
        if schema.get("additionalProperties") is False:
            declared = set(schema.get("properties", {}).keys())
            for key in sorted(set(value.keys()) - declared):
                errors.append(f"{path}: additional property '{key}' is not allowed")

    elif isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema:
            for idx, item in enumerate(value):
                errors.extend(_validate(item, item_schema, f"{path}[{idx}]"))
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: array length {len(value)} < minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: array length {len(value)} > maxItems {schema['maxItems']}")

    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: string length {len(value)} < minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: string length {len(value)} > maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match pattern {schema['pattern']!r}")

    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: value {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: value {value} > maximum {schema['maximum']}")

    return errors


def _inject_no_additional(schema: dict[str, Any]) -> None:
    if schema.get("type") == "object" or "properties" in schema:
        schema.setdefault("additionalProperties", False)
    for sub in schema.get("properties", {}).values():
        if isinstance(sub, dict):
            _inject_no_additional(sub)
    items = schema.get("items")
    if isinstance(items, dict):
        _inject_no_additional(items)