"""
core/redis_client.py
====================
Shared Redis connection pool + helper methods for Project Aegis.

Provides a singleton ``RedisClient`` via ``get_redis()`` that all modules
import.  Three key namespaces are pre-wired:

    aegis:hash:{namespace}:{sha256}   — document deduplication (Phase 1/2)
    aegis:sla:{ticket_id}            — SLA state cache         (Phase 1)
    aegis:tension:{tension_id}       — conflict records         (Phase 3)

Failure mode: all methods catch ``redis.exceptions.ConnectionError`` and
return ``False`` / ``None`` — they NEVER raise.  This keeps fetch_rbi.py
and task_manager.py fully functional when Redis is down.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional import — module works (returns no-op) even if redis is not installed
# ---------------------------------------------------------------------------
try:
    import redis as _redis
    _REDIS_AVAILABLE = True
except ImportError:
    _redis = None  # type: ignore[assignment]
    _REDIS_AVAILABLE = False
    logger.warning(
        "redis-py not installed (pip install redis hiredis). "
        "RedisClient will operate in no-op mode."
    )


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Configuration (from environment)
# ---------------------------------------------------------------------------

REDIS_URL = _env("REDIS_URL", "redis://localhost:6379/0")
REDIS_HASH_TTL_DAYS = _env_int("REDIS_HASH_TTL_DAYS", 90)
REDIS_SLA_TTL_DAYS = _env_int("REDIS_SLA_TTL_DAYS", 60)
REDIS_TENSION_TTL_DAYS = _env_int("REDIS_TENSION_TTL_DAYS", 180)

# Seconds
_HASH_TTL = REDIS_HASH_TTL_DAYS * 86400
_SLA_TTL = REDIS_SLA_TTL_DAYS * 86400
_TENSION_TTL = REDIS_TENSION_TTL_DAYS * 86400


# ---------------------------------------------------------------------------
# RedisClient
# ---------------------------------------------------------------------------

class RedisClient:
    """
    Thin wrapper around ``redis-py`` with Aegis-specific key namespaces.

    All public methods swallow ``ConnectionError`` and return safe defaults
    so callers never need try/except around Redis calls.
    """

    def __init__(self) -> None:
        self._pool: Any = None
        self._client: Any = None

        if not _REDIS_AVAILABLE:
            logger.warning("RedisClient initialised in no-op mode (redis-py not installed).")
            return

        try:
            self._pool = _redis.ConnectionPool.from_url(
                REDIS_URL,
                max_connections=20,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            self._client = _redis.Redis(connection_pool=self._pool)
            # Quick connectivity check
            self._client.ping()
            logger.info("RedisClient connected to %s", REDIS_URL)
        except Exception as exc:
            logger.warning(
                "RedisClient could not connect to %s (%s). "
                "Will return no-op responses until Redis becomes available.",
                REDIS_URL, exc,
            )
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_available(self) -> bool:
        return self._client is not None

    def _safe_get(self, key: str) -> str | None:
        """GET with connection-error safety."""
        if not self._is_available():
            return None
        try:
            return self._client.get(key)
        except Exception as exc:
            logger.warning("Redis GET failed for key=%s: %s", key, exc)
            return None

    def _safe_set(self, key: str, value: str, ttl: int) -> bool:
        """SET with EX and connection-error safety."""
        if not self._is_available():
            return False
        try:
            self._client.set(key, value, ex=ttl)
            return True
        except Exception as exc:
            logger.warning("Redis SET failed for key=%s: %s", key, exc)
            return False

    # ── Hash deduplication ────────────────────────────────────────────────

    def hash_exists(self, sha256: str, namespace: str) -> bool:
        """
        Check if a document hash is already registered.

        Key pattern: ``aegis:hash:{namespace}:{sha256}``
        Returns ``True`` if key exists, ``False`` on miss or error.
        """
        key = f"aegis:hash:{namespace}:{sha256}"
        if not self._is_available():
            return False
        try:
            return bool(self._client.exists(key))
        except Exception as exc:
            logger.warning("Redis EXISTS failed for key=%s: %s", key, exc)
            return False

    def hash_register(self, sha256: str, namespace: str, meta: dict) -> None:
        """
        Register a document hash with JSON-serialised metadata.

        Key pattern: ``aegis:hash:{namespace}:{sha256}``
        TTL: ``REDIS_HASH_TTL_DAYS`` (default 90 days).
        """
        key = f"aegis:hash:{namespace}:{sha256}"
        self._safe_set(key, json.dumps(meta, default=str), _HASH_TTL)

    # ── SLA state cache ───────────────────────────────────────────────────

    def sla_set(self, ticket_id: str, state: dict) -> None:
        """
        Cache the current SLA state for a ticket.

        Key pattern: ``aegis:sla:{ticket_id}``
        TTL: ``REDIS_SLA_TTL_DAYS`` (default 60 days).
        """
        key = f"aegis:sla:{ticket_id}"
        self._safe_set(key, json.dumps(state, default=str), _SLA_TTL)

    def sla_get(self, ticket_id: str) -> dict | None:
        """
        Retrieve cached SLA state for a ticket.

        Returns deserialised dict or ``None`` if expired/missing/error.
        """
        key = f"aegis:sla:{ticket_id}"
        raw = self._safe_get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    # ── Tension records (Phase 3 pre-wired) ───────────────────────────────

    def tension_set(self, tension_id: str, record: dict) -> None:
        """
        Store a conflict/tension record.

        Key pattern: ``aegis:tension:{tension_id}``
        TTL: ``REDIS_TENSION_TTL_DAYS`` (default 180 days).
        """
        key = f"aegis:tension:{tension_id}"
        self._safe_set(key, json.dumps(record, default=str), _TENSION_TTL)

    def tension_get(self, tension_id: str) -> dict | None:
        """
        Retrieve a single tension record by ID.

        Returns deserialised dict or ``None``.
        """
        key = f"aegis:tension:{tension_id}"
        raw = self._safe_get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def tension_scan(self) -> list[dict]:
        """
        Scan all ``aegis:tension:*`` keys and return their values.

        Uses ``SCAN`` for memory-safe iteration (no ``KEYS *``).
        Returns an empty list on any error.
        """
        if not self._is_available():
            return []
        results: list[dict] = []
        try:
            cursor = 0
            while True:
                cursor, keys = self._client.scan(
                    cursor=cursor, match="aegis:tension:*", count=100
                )
                for key in keys:
                    raw = self._client.get(key)
                    if raw:
                        try:
                            results.append(json.loads(raw))
                        except (json.JSONDecodeError, TypeError):
                            continue
                if cursor == 0:
                    break
        except Exception as exc:
            logger.warning("Redis SCAN failed for aegis:tension:*: %s", exc)
        return results


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_singleton: RedisClient | None = None


def get_redis() -> RedisClient:
    """
    Return the process-wide ``RedisClient`` singleton.

    Safe to call from any thread / coroutine — the underlying ``redis-py``
    connection pool is thread-safe.
    """
    global _singleton
    if _singleton is None:
        _singleton = RedisClient()
    return _singleton
