"""
validation/validator.py

Automated Hybrid Validation Engine
────────────────────────────────────
A 2-tier validation system that combines:

  Tier 1 – Deterministic checks
    • ESB API calls  → verify live IT configuration (e.g. AES-256 encryption)
    • Azure Key Vault → fetch secrets/config metadata for policy alignment

  Tier 2 – Document Intelligence checks
    • SHA-256 hash integrity + tamper / duplicate / backdate detection
    • SharePoint document version increment verification
    • Keyword / policy-term presence scan

Post-validation:
    • PASS / FAIL with confidence score (0.0–1.0) and explanation
    • On FAIL → rework loop trigger, SLA reset to 2 hours, Teams notification

Architecture
────────────
  ┌──────────────────────────────────────┐
  │          HybridValidator             │
  │                                      │
  │  ┌─────────────────────────────────┐ │
  │  │  Tier 1 – DeterministicEngine   │ │
  │  │  ┌────────────┐ ┌────────────┐  │ │
  │  │  │ ESBChecker │ │ KeyVault   │  │ │
  │  │  │            │ │ Checker    │  │ │
  │  │  └────────────┘ └────────────┘  │ │
  │  └─────────────────────────────────┘ │
  │  ┌─────────────────────────────────┐ │
  │  │  Tier 2 – DocIntelligenceEngine │ │
  │  │  ┌──────────┐ ┌─────────────┐  │ │
  │  │  │ HashGuard│ │ SharePoint  │  │ │
  │  │  │          │ │ Inspector   │  │ │
  │  │  └──────────┘ └─────────────┘  │ │
  │  └─────────────────────────────────┘ │
  │  ┌─────────────────────────────────┐ │
  │  │  ReworkOrchestrator             │ │
  │  │  (SLA reset + Teams notify)     │ │
  │  └─────────────────────────────────┘ │
  └──────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
import httpx

# ── Phase 6: Intelligent validation layer ─────────────────────────────────────
try:
    from validation.intelligent_checks import (
        AnomalyDetector,
        ScreenshotOCRVerifier,
        ForgeryDetector,
        ComplianceConfidenceScorer,
        AnomalyReport,
        OCRVerificationResult,
        ForgeryReport,
        ConfidenceBreakdown,
    )
    _INTELLIGENT_CHECKS_AVAILABLE = True
except ImportError as _ic_err:
    _INTELLIGENT_CHECKS_AVAILABLE = False
    logger = logging.getLogger("validator")
    logging.getLogger("validator").warning("intelligent_checks unavailable: %s", _ic_err)

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger("validator")

# ── Phase 1: Redis connection singleton ─────────────────────────────────────
try:
    from core.redis_client import get_redis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
PASS_CONFIDENCE_THRESHOLD  = 0.80   # overall score must exceed this to PASS
REWORK_SLA_HOURS           = 2      # SLA hours granted after rework trigger
HASH_REGISTRY_MAX_AGE_DAYS = 365    # how far back we check for duplicates
BACKDATE_TOLERANCE_MINUTES = 5      # clock skew tolerance for submission time


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class ValidationOutcome(str, Enum):
    PASS            = "PASS"
    FAIL            = "FAIL"
    REWORK          = "REWORK"          # FAIL + rework loop triggered
    FRAUD_SUSPECTED = "FRAUD_SUSPECTED"  # ForgeryDetector flagged HIGH risk
    ANOMALY_DETECTED = "ANOMALY_DETECTED" # AnomalyDetector flagged HIGH risk


class CheckCategory(str, Enum):
    ESB         = "ESB_API"
    KEY_VAULT   = "KEY_VAULT"
    HASH_GUARD  = "HASH_GUARD"
    SHAREPOINT  = "SHAREPOINT"


# ─────────────────────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    check_id:    str
    category:   CheckCategory
    name:       str
    passed:     bool
    confidence: float        # 0.0–1.0
    explanation: str
    evidence:   dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "check_id":    self.check_id,
            "category":    self.category.value,
            "name":        self.name,
            "passed":      self.passed,
            "confidence":  round(self.confidence, 4),
            "explanation": self.explanation,
            "evidence":    self.evidence,
        }


@dataclass
class ReworkRecord:
    rework_id:       str
    task_id:         str
    triggered_at:    str
    sla_deadline:    str
    failed_checks:   list[str]          # check names
    notified_to:     str                # Teams handle / email
    notification_sent: bool

    def to_dict(self) -> dict:
        return {
            "rework_id":         self.rework_id,
            "task_id":           self.task_id,
            "triggered_at":      self.triggered_at,
            "sla_deadline":      self.sla_deadline,
            "failed_checks":     self.failed_checks,
            "notified_to":       self.notified_to,
            "notification_sent": self.notification_sent,
        }


@dataclass
class ValidationReport:
    report_id:       str
    task_id:         str
    obligation_id:   str
    department:      str
    unit_head_email: str
    outcome:         ValidationOutcome
    overall_confidence: float
    summary:         str
    check_results:   list[CheckResult]
    rework:          ReworkRecord | None
    evidence_hash:   str              # SHA-256 of all serialised evidence
    validated_at:    str
    # Phase 6 intelligent check results
    anomaly_report:       Optional[dict[str, Any]] = None
    forgery_report:       Optional[dict[str, Any]] = None
    ocr_result:           Optional[dict[str, Any]] = None
    confidence_breakdown: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {
            "report_id":            self.report_id,
            "task_id":              self.task_id,
            "obligation_id":        self.obligation_id,
            "department":           self.department,
            "unit_head_email":      self.unit_head_email,
            "outcome":              self.outcome.value,
            "overall_confidence":   round(self.overall_confidence, 4),
            "summary":              self.summary,
            "check_results":        [c.to_dict() for c in self.check_results],
            "rework":               self.rework.to_dict() if self.rework else None,
            "evidence_hash":        self.evidence_hash,
            "validated_at":         self.validated_at,
            "anomaly_report":       self.anomaly_report,
            "forgery_report":       self.forgery_report,
            "ocr_result":           self.ocr_result,
            "confidence_breakdown": self.confidence_breakdown,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Redis-backed hash registry (replaces local in-memory dict)
# ─────────────────────────────────────────────────────────────────────────────

class _HashRegistry:
    """Redis-backed store of seen evidence hashes with automatic expiration.
    Falls back to a local in-memory registry if Redis is unreachable or down.
    """

    def __init__(self) -> None:
        self._local_fallback: dict[str, datetime] = {}

    def is_duplicate(self, sha256: str) -> bool:
        if not _REDIS_AVAILABLE or not get_redis()._is_available():
            return sha256 in self._local_fallback
        try:
            return get_redis().hash_exists(sha256, "validation")
        except Exception as exc:
            logger.warning("Redis duplicate check failed for hash=%s, using fallback: %s", sha256, exc)
            return sha256 in self._local_fallback

    def register(self, sha256: str, when: datetime) -> None:
        self._local_fallback[sha256] = when
        if not _REDIS_AVAILABLE or not get_redis()._is_available():
            return
        try:
            get_redis().hash_register(
                sha256, 
                "validation", 
                {"first_seen": when.isoformat()}
            )
        except Exception as exc:
            logger.warning("Redis register failed for hash=%s: %s", sha256, exc)

    def first_seen(self, sha256: str) -> datetime | None:
        if not _REDIS_AVAILABLE or not get_redis()._is_available():
            return self._local_fallback.get(sha256)
        try:
            key = f"aegis:hash:validation:{sha256}"
            raw = get_redis()._safe_get(key)
            if raw:
                meta = json.loads(raw)
                first_seen_str = meta.get("first_seen")
                if first_seen_str:
                    return datetime.fromisoformat(first_seen_str)
        except Exception as exc:
            logger.warning("Redis query failed for hash=%s, using fallback: %s", sha256, exc)
        return self._local_fallback.get(sha256)


_HASH_REGISTRY = _HashRegistry()


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1 – Deterministic Engine
# ─────────────────────────────────────────────────────────────────────────────

class ESBChecker:
    """
    Calls the Enterprise Service Bus API to verify live IT configuration.
    Replace `_esb_client` with your actual ESB / REST client.
    """

    def __init__(self, esb_base_url: str = "https://esb.internal/api/v1") -> None:
        self._base_url = esb_base_url
        self._client = httpx.AsyncClient(timeout=30, verify=True)

    async def check_encryption(self, system_id: str) -> CheckResult:
        """Verify AES-256 encryption is enabled on the target system."""
        endpoint = f"{self._base_url}/systems/{system_id}/encryption"
        try:
            resp = await self._client.get(endpoint)
            resp.raise_for_status()
            data: dict = resp.json()
            enabled    = data.get("enabled", False)
            algorithm  = data.get("algorithm", "").upper()
            key_size   = int(data.get("key_size", 0))

            ok      = enabled and "AES" in algorithm and key_size >= 256
            conf    = 0.98 if ok else 0.10
            msg     = (
                f"AES-256 encryption ACTIVE (algorithm={algorithm}, mode={data.get('mode')})."
                if ok else
                f"AES-256 encryption check FAILED (enabled={enabled}, algorithm={algorithm}, key_size={key_size})."
            )
            return CheckResult(
                check_id=_new_id(), category=CheckCategory.ESB,
                name="ESB:encryption_check",
                passed=ok, confidence=conf, explanation=msg,
                evidence={"system_id": system_id, **data},
            )
        except Exception as exc:
            return _error_check(CheckCategory.ESB, "ESB:encryption_check", exc)

    async def check_tls(self, system_id: str) -> CheckResult:
        """Verify TLS 1.3 is strictly enforced on the target system."""
        endpoint = f"{self._base_url}/systems/{system_id}/tls"
        try:
            resp = await self._client.get(endpoint)
            resp.raise_for_status()
            data: dict = resp.json()
            version = data.get("tls_version", "")
            ok      = version == "TLSv1.3"   # strict TLS 1.3 only
            conf    = 0.97 if ok else 0.15
            msg     = (
                f"TLS configuration VALID ({version}, cipher={data.get('cipher')})."
                if ok else
                f"TLS configuration INVALID (version={version}). TLS 1.3 is required."
            )
            return CheckResult(
                check_id=_new_id(), category=CheckCategory.ESB,
                name="ESB:tls_check",
                passed=ok, confidence=conf, explanation=msg,
                evidence={"system_id": system_id, **data},
            )
        except Exception as exc:
            return _error_check(CheckCategory.ESB, "ESB:tls_check", exc)

    async def check_access_control(self, system_id: str) -> CheckResult:
        """Verify MFA and RBAC are enabled."""
        endpoint = f"{self._base_url}/systems/{system_id}/access-control"
        try:
            resp = await self._client.get(endpoint)
            resp.raise_for_status()
            data: dict = resp.json()
            mfa  = data.get("mfa_enabled", False)
            rbac = data.get("rbac_enabled", False)
            ok   = mfa and rbac
            conf = 0.96 if ok else 0.20
            msg  = (
                f"Access controls VALID (MFA={mfa}, RBAC={rbac})."
                if ok else
                f"Access controls INVALID (MFA={mfa}, RBAC={rbac})."
            )
            return CheckResult(
                check_id=_new_id(), category=CheckCategory.ESB,
                name="ESB:access_control_check",
                passed=ok, confidence=conf, explanation=msg,
                evidence={"system_id": system_id, **data},
            )
        except Exception as exc:
            return _error_check(CheckCategory.ESB, "ESB:access_control_check", exc)


class KeyVaultChecker:
    """
    Fetches secret / configuration metadata from Azure Key Vault to verify
    policy alignment (e.g. key rotation schedules, allowed algorithms).
    Replace the mock with `azure.keyvault.secrets.SecretClient`.
    """

    def __init__(
        self,
        vault_url: str = "https://compliance-vault.vault.azure.net/",
    ) -> None:
        self._vault_url = vault_url
        self._client = None
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
            credential = DefaultAzureCredential()
            self._client = SecretClient(vault_url=vault_url, credential=credential)
            logger.info("KeyVaultChecker connected to %s", vault_url)
        except Exception as exc:
            logger.warning("Key Vault client init failed: %s", exc)

    async def verify_key_policy(self, secret_name: str) -> CheckResult:
        """
        Verify that the vault secret is enabled, not expired, and
        tagged with an approved algorithm.
        """
        if not self._client:
            return _error_check(CheckCategory.KEY_VAULT, "KV:key_policy_check",
                                RuntimeError("Key Vault client not initialised"))
        try:
            secret    = self._client.get_secret(secret_name)
            props     = secret.properties
            enabled   = props.enabled
            expires   = props.expires_on
            now       = datetime.now(tz=timezone.utc)
            not_expired = expires is None or expires > now
            tags        = props.tags or {}
            algorithm   = tags.get("algorithm", "").upper()
            algo_ok     = "AES-256" in algorithm or "RSA-4096" in algorithm

            ok   = enabled and not_expired and algo_ok
            conf = 0.97 if ok else 0.15
            msg  = (
                f"Key Vault secret '{secret_name}' VALID "
                f"(enabled={enabled}, expires={expires}, algorithm={algorithm})."
                if ok else
                f"Key Vault secret '{secret_name}' INVALID "
                f"(enabled={enabled}, expired={not not_expired}, algorithm='{algorithm}')."
            )
            return CheckResult(
                check_id=_new_id(), category=CheckCategory.KEY_VAULT,
                name="KV:key_policy_check",
                passed=ok, confidence=conf, explanation=msg,
                evidence={
                    "secret_name": secret_name,
                    "enabled":     enabled,
                    "expires_on":  expires.isoformat() if expires else None,
                    "algorithm":   algorithm,
                },
            )
        except Exception as exc:
            return _error_check(CheckCategory.KEY_VAULT, "KV:key_policy_check", exc)

    async def verify_rotation_schedule(self, secret_name: str) -> CheckResult:
        """Verify that key rotation is scheduled within policy limits (≤ 90 days)."""
        if not self._client:
            return _error_check(CheckCategory.KEY_VAULT, "KV:rotation_schedule_check",
                                RuntimeError("Key Vault client not initialised"))
        try:
            secret          = self._client.get_secret(secret_name)
            tags            = secret.properties.tags or {}
            rotation_days   = int(tags.get("rotation_days", 999))
            ok              = rotation_days <= 90
            conf            = 0.95 if ok else 0.20
            msg             = (
                f"Key rotation schedule COMPLIANT ({rotation_days} days ≤ 90)."
                if ok else
                f"Key rotation schedule NON-COMPLIANT ({rotation_days} days > 90 days policy limit)."
            )
            return CheckResult(
                check_id=_new_id(), category=CheckCategory.KEY_VAULT,
                name="KV:rotation_schedule_check",
                passed=ok, confidence=conf, explanation=msg,
                evidence={"secret_name": secret_name, "rotation_days": rotation_days},
            )
        except Exception as exc:
            return _error_check(CheckCategory.KEY_VAULT, "KV:rotation_schedule_check", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2 – Document Intelligence Engine
# ─────────────────────────────────────────────────────────────────────────────

class HashGuard:
    """
    Tamper-proof evidence layer.
    1. Calculates SHA-256 of submitted content.
    2. Detects duplicate submissions (same hash seen before).
    3. Detects backdated submissions (claimed timestamp < actual receipt).
    """

    def check(
        self,
        content: bytes,
        claimed_timestamp: datetime | None = None,
    ) -> tuple[str, CheckResult]:
        """
        Returns (sha256_hex, CheckResult).
        Caller should store the sha256 in the final report.
        """
        sha256      = hashlib.sha256(content).hexdigest()
        now         = datetime.now(tz=timezone.utc)
        issues: list[str] = []

        # ── Duplicate detection ───────────────────────────────────────────────
        if _HASH_REGISTRY.is_duplicate(sha256):
            first = _HASH_REGISTRY.first_seen(sha256)
            issues.append(
                f"DUPLICATE submission detected (first seen {first.isoformat()})."
            )

        # ── Backdate detection ────────────────────────────────────────────────
        if claimed_timestamp:
            claimed_utc = claimed_timestamp.astimezone(timezone.utc)
            delta       = now - claimed_utc
            if delta.total_seconds() < -BACKDATE_TOLERANCE_MINUTES * 60:
                issues.append(
                    f"BACKDATED submission: claimed={claimed_utc.isoformat()}, "
                    f"received={now.isoformat()} (delta={delta})."
                )

        _HASH_REGISTRY.register(sha256, now)

        passed = len(issues) == 0
        conf   = 0.99 if passed else 0.05
        msg    = (
            f"Evidence integrity OK (SHA-256={sha256[:16]}…)."
            if passed else
            " | ".join(issues)
        )
        result = CheckResult(
            check_id=_new_id(), category=CheckCategory.HASH_GUARD,
            name="HashGuard:integrity_check",
            passed=passed, confidence=conf, explanation=msg,
            evidence={"sha256": sha256, "issues": issues},
        )
        return sha256, result


class SharePointInspector:
    """
    Verifies policy documents stored in SharePoint:
      1. Version increment – new version must be > previous version.
      2. Keyword / policy-term presence scan.
    Replace the mock with the `Office365-REST-Python-Client` or MS Graph SDK.
    """

    # Policy terms that MUST appear in a compliant policy document
    REQUIRED_KEYWORDS: list[str] = [
        r"data\s+protection",
        r"encryption",
        r"access\s+control",
        r"incident\s+response",
        r"retention\s+period",
        r"GDPR|DPDPA|PCI\s*DSS|ISO\s*27001",
        r"review\s+cycle|reviewed\s+by",
        r"version\s+\d+\.\d+",
    ]

    def __init__(
        self,
        site_url: str = "https://org.sharepoint.com/sites/compliance",
    ) -> None:
        self._site_url = site_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=30, verify=True)

    async def _get_file_metadata(self, file_path: str) -> dict:
        """Fetch file metadata via SharePoint REST API."""
        url = (
            f"{self._site_url}/_api/web/GetFileByServerRelativeUrl"
            f"('{file_path}')"
        )
        resp = await self._client.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json().get("d", resp.json())

    async def _get_file_content(self, file_path: str) -> bytes:
        """Download file content via SharePoint REST API."""
        url = (
            f"{self._site_url}/_api/web/GetFileByServerRelativeUrl"
            f"('{file_path}')/$value"
        )
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.content

    async def check_version_increment(
        self,
        file_path: str,
        previous_major: int,
        previous_minor: int = 0,
    ) -> CheckResult:
        """Verify the document version has been incremented since last approval."""
        try:
            meta  = await self._get_file_metadata(file_path)
            major = int(meta.get("MajorVersion", 0))
            minor = int(meta.get("MinorVersion", 0))

            prev_tuple = (previous_major, previous_minor)
            curr_tuple = (major, minor)
            ok   = curr_tuple > prev_tuple
            conf = 0.96 if ok else 0.10
            msg  = (
                f"Version INCREMENTED from {previous_major}.{previous_minor} "
                f"to {major}.{minor} in '{meta.get('Name')}'."
                if ok else
                f"Version NOT incremented: current={major}.{minor}, "
                f"previous={previous_major}.{previous_minor}."
            )
            return CheckResult(
                check_id=_new_id(), category=CheckCategory.SHAREPOINT,
                name="SP:version_increment_check",
                passed=ok, confidence=conf, explanation=msg,
                evidence={
                    "file":            file_path,
                    "current_version": f"{major}.{minor}",
                    "previous_version": f"{previous_major}.{previous_minor}",
                    "author":          meta.get("Author"),
                    "last_modified":   meta.get("TimeLastModified"),
                },
            )
        except Exception as exc:
            return _error_check(CheckCategory.SHAREPOINT, "SP:version_increment_check", exc)

    async def check_required_keywords(self, file_path: str) -> CheckResult:
        """Scan document for mandatory policy keywords / terms."""
        try:
            content_bytes = await self._get_file_content(file_path)
            text          = content_bytes.decode(errors="replace")

            missing: list[str] = []
            found:   list[str] = []
            for pattern in self.REQUIRED_KEYWORDS:
                if re.search(pattern, text, re.I):
                    found.append(pattern)
                else:
                    missing.append(pattern)

            total   = len(self.REQUIRED_KEYWORDS)
            hit_rate = len(found) / total if total else 0.0
            ok       = len(missing) == 0
            conf     = round(0.50 + hit_rate * 0.49, 4)   # 0.50 → 0.99

            msg = (
                f"All {total} required policy keywords found."
                if ok else
                f"Missing {len(missing)}/{total} keywords: {missing}."
            )
            return CheckResult(
                check_id=_new_id(), category=CheckCategory.SHAREPOINT,
                name="SP:keyword_scan_check",
                passed=ok, confidence=conf, explanation=msg,
                evidence={
                    "file":     file_path,
                    "found":    found,
                    "missing":  missing,
                    "hit_rate": round(hit_rate, 4),
                },
            )
        except Exception as exc:
            return _error_check(CheckCategory.SHAREPOINT, "SP:keyword_scan_check", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Rework Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class ReworkOrchestrator:
    """
    On validation failure:
      1. Creates a rework record with a 2-hour SLA deadline.
      2. Sends a Teams adaptive-card notification to the unit head.
    Replace `_teams_client` with the actual MS Teams webhook / Graph SDK.
    """

    def __init__(
        self,
        teams_webhook_url: str = "https://outlook.office.com/webhook/PLACEHOLDER",
    ) -> None:
        self._webhook_url = teams_webhook_url
        self._client = httpx.AsyncClient(timeout=15, verify=True)

    async def trigger(
        self,
        task_id:         str,
        obligation_id:   str,
        unit_head_email: str,
        unit_head_name:  str,
        failed_checks:   list[CheckResult],
        department:      str,
    ) -> ReworkRecord:
        now          = datetime.now(tz=timezone.utc)
        sla_deadline = now + timedelta(hours=REWORK_SLA_HOURS)
        rework_id    = _new_id()
        fail_names   = [c.name for c in failed_checks]
        fail_detail  = "\n".join(
            f"  • {c.name}: {c.explanation}" for c in failed_checks
        )

        payload = self._build_teams_card(
            rework_id=rework_id,
            task_id=task_id,
            obligation_id=obligation_id,
            unit_head_name=unit_head_name,
            department=department,
            fail_detail=fail_detail,
            sla_deadline=sla_deadline,
        )

        try:
            await self._client.post(self._webhook_url, json=payload)
            sent = True
            logger.info(
                "Rework notification sent | task=%s | to=%s | deadline=%s",
                task_id, unit_head_email, sla_deadline.isoformat(),
            )
        except Exception as exc:
            sent = False
            logger.error("Teams notification FAILED: %s", exc)

        return ReworkRecord(
            rework_id=rework_id,
            task_id=task_id,
            triggered_at=now.isoformat(),
            sla_deadline=sla_deadline.isoformat(),
            failed_checks=fail_names,
            notified_to=unit_head_email,
            notification_sent=sent,
        )

    @staticmethod
    def _build_teams_card(
        rework_id:       str,
        task_id:         str,
        obligation_id:   str,
        unit_head_name:  str,
        department:      str,
        fail_detail:     str,
        sla_deadline:    datetime,
    ) -> dict:
        """Adaptive Card payload for Microsoft Teams."""
        return {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type":    "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type":   "TextBlock",
                            "size":   "Large",
                            "weight": "Bolder",
                            "color":  "Attention",
                            "text":   "⚠️ Compliance Validation FAILED – Rework Required",
                        },
                        {
                            "type":  "FactSet",
                            "facts": [
                                {"title": "Rework ID",      "value": rework_id},
                                {"title": "Task ID",        "value": task_id},
                                {"title": "Obligation",     "value": obligation_id},
                                {"title": "Department",     "value": department},
                                {"title": "Assigned To",    "value": unit_head_name},
                                {"title": "SLA Deadline",   "value": sla_deadline.strftime("%Y-%m-%d %H:%M UTC")},
                                {"title": "SLA Window",     "value": f"{REWORK_SLA_HOURS} hours"},
                            ],
                        },
                        {
                            "type":   "TextBlock",
                            "weight": "Bolder",
                            "text":   "Failed Checks:",
                        },
                        {
                            "type":  "TextBlock",
                            "wrap":  True,
                            "text":  fail_detail or "See validation report.",
                        },
                    ],
                    "actions": [{
                        "type":  "Action.OpenUrl",
                        "title": "View Full Validation Report",
                        "url":   f"https://compliance.internal/reports/{rework_id}",
                    }],
                },
            }],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Hybrid Validator (Orchestrator)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationRequest:
    """
    Encapsulates everything needed to validate a single compliance task.

    Fields
    ──────
    task_id          : ID from the routing layer (RoutedTask.task_id)
    obligation_id    : Back-reference to the obligation map
    department       : Target department name
    unit_head_email  : For rework notifications
    unit_head_name   : Human-readable name for the Teams card
    system_id        : ESB system identifier (for IT checks)
    vault_secret     : Azure Key Vault secret name
    document_path    : SharePoint-relative document path
    document_content : Raw bytes of the document (for hashing)
    previous_doc_version : (major, minor) of the last approved version
    claimed_timestamp    : Timestamp the submitter claims for the evidence
    run_esb_checks   : Toggle Tier-1 ESB checks
    run_kv_checks    : Toggle Tier-1 Key Vault checks
    run_doc_checks   : Toggle Tier-2 document checks
    """
    task_id:              str
    obligation_id:        str
    department:           str
    unit_head_email:      str
    unit_head_name:       str
    system_id:            str              = "sys-001"
    vault_secret:         str              = "encryption-key"
    document_path:        str              = "/policies/DataProtectionPolicy.docx"
    document_content:     bytes            = b""
    previous_doc_version: tuple[int, int]  = (2, 0)
    claimed_timestamp:    datetime | None  = None
    run_esb_checks:       bool             = True
    run_kv_checks:        bool             = True
    run_doc_checks:       bool             = True
    # Phase 6 intelligent check inputs
    screenshot_bytes:     bytes            = b""     # PNG/JPEG config screenshot
    document_bytes_pdf:   bytes            = b""     # PDF for forgery detection
    claimed_compliance_date: Any          = None     # date object
    obligation_text:      str             = ""       # for OCR comparison
    regulation_ref:       str             = ""       # for temporal forgery check
    run_intelligent_checks: bool          = True


class HybridValidator:
    """
    Orchestrates all validation tiers.

    Usage::

        validator = HybridValidator()
        report    = await validator.validate(request)
        print(json.dumps(report.to_dict(), indent=2))
    """

    def __init__(
        self,
        esb_base_url:      str = "https://esb.internal/api/v1",
        vault_url:         str = "https://compliance-vault.vault.azure.net/",
        sharepoint_url:    str = "https://org.sharepoint.com/sites/compliance",
        teams_webhook_url: str = "https://outlook.office.com/webhook/PLACEHOLDER",
    ) -> None:
        self._esb        = ESBChecker(esb_base_url)
        self._kv         = KeyVaultChecker(vault_url)
        self._hash_guard = HashGuard()
        self._sp         = SharePointInspector(sharepoint_url)
        self._rework     = ReworkOrchestrator(teams_webhook_url)
        # Phase 6 intelligent checkers
        if _INTELLIGENT_CHECKS_AVAILABLE:
            self._anomaly  = AnomalyDetector()
            self._ocr      = ScreenshotOCRVerifier()
            self._forgery  = ForgeryDetector()
            self._conf_scorer = ComplianceConfidenceScorer()
        else:
            self._anomaly = self._ocr = self._forgery = self._conf_scorer = None

    async def validate(self, req: ValidationRequest) -> ValidationReport:
        """
        Run both tiers, aggregate results, compute overall confidence,
        and trigger rework if any check fails.
        """
        all_checks: list[CheckResult] = []
        evidence_blob: dict[str, Any] = {}

        # ── Tier 1a: ESB checks ───────────────────────────────────────────────
        if req.run_esb_checks:
            esb_results = await asyncio.gather(
                self._esb.check_encryption(req.system_id),
                self._esb.check_tls(req.system_id),
                self._esb.check_access_control(req.system_id),
            )
            all_checks.extend(esb_results)
            for r in esb_results:
                evidence_blob[r.name] = r.evidence

        # ── Tier 1b: Key Vault checks ─────────────────────────────────────────
        if req.run_kv_checks:
            kv_results = await asyncio.gather(
                self._kv.verify_key_policy(req.vault_secret),
                self._kv.verify_rotation_schedule(req.vault_secret),
            )
            all_checks.extend(kv_results)
            for r in kv_results:
                evidence_blob[r.name] = r.evidence

        # ── Tier 2a: Hash Guard (integrity / tamper) ──────────────────────────
        content = req.document_content or b"[no document content provided]"
        sha256, hash_result = self._hash_guard.check(
            content=content,
            claimed_timestamp=req.claimed_timestamp,
        )
        all_checks.append(hash_result)
        evidence_blob["HashGuard"] = hash_result.evidence

        # ── Tier 2b: SharePoint document intelligence ─────────────────────────
        if req.run_doc_checks:
            sp_results = await asyncio.gather(
                self._sp.check_version_increment(
                    req.document_path,
                    req.previous_doc_version[0],
                    req.previous_doc_version[1],
                ),
                self._sp.check_required_keywords(req.document_path),
            )
            all_checks.extend(sp_results)
            for r in sp_results:
                evidence_blob[r.name] = r.evidence

        # ── Phase 6a: Anomaly Detection ───────────────────────────────────────
        anomaly_report_dict = None
        if req.run_intelligent_checks and self._anomaly:
            doc_size = len(req.document_content or req.document_bytes_pdf or b"")
            anomaly  = self._anomaly.detect(
                doc_size_bytes       = doc_size,
                submission_ts        = datetime.now(tz=timezone.utc),
                prior_failed_checks  = sum(1 for c in all_checks if not c.passed),
            )
            anomaly_report_dict = anomaly.to_dict()
            if anomaly.risk_level == "HIGH":
                all_checks.append(CheckResult(
                    check_id=_new_id(), category=CheckCategory.HASH_GUARD,
                    name="Anomaly:high_risk", passed=False, confidence=0.15,
                    explanation=f"AnomalyDetector HIGH: {'; '.join(anomaly.suspicious_features[:2])}",
                    evidence=anomaly_report_dict,
                ))

        # ── Phase 6b: Screenshot OCR Verification ────────────────────────────
        ocr_result_dict = None
        if req.run_intelligent_checks and self._ocr and req.screenshot_bytes:
            ocr = self._ocr.verify(
                image_bytes     = req.screenshot_bytes,
                obligation_text = req.obligation_text,
            )
            ocr_result_dict = ocr.to_dict()
            if not ocr.passed:
                all_checks.append(CheckResult(
                    check_id=_new_id(), category=CheckCategory.ESB,
                    name="OCR:config_mismatch", passed=False, confidence=ocr.confidence,
                    explanation=f"OCR mismatch: {'; '.join(ocr.mismatches[:2])}",
                    evidence=ocr_result_dict,
                ))

        # ── Phase 6c: Forgery Detection ───────────────────────────────────────
        forgery_report_dict = None
        if req.run_intelligent_checks and self._forgery and req.document_bytes_pdf:
            forgery = self._forgery.detect(
                document_bytes           = req.document_bytes_pdf,
                claimed_compliance_date  = req.claimed_compliance_date,
                regulation_ref           = req.regulation_ref,
            )
            forgery_report_dict = forgery.to_dict()
            if forgery.risk_level == "HIGH":
                all_checks.append(CheckResult(
                    check_id=_new_id(), category=CheckCategory.HASH_GUARD,
                    name="Forgery:high_risk", passed=False, confidence=0.10,
                    explanation=f"ForgeryDetector HIGH: {'; '.join(forgery.findings[:2])}",
                    evidence=forgery_report_dict,
                ))

        # ── Aggregate confidence & outcome ────────────────────────────────────
        overall_confidence = self._aggregate_confidence(all_checks)
        failed_checks      = [c for c in all_checks if not c.passed]
        all_passed         = len(failed_checks) == 0 and overall_confidence >= PASS_CONFIDENCE_THRESHOLD

        # Phase 6d: Confidence Breakdown
        conf_breakdown_dict = None
        if self._conf_scorer:
            esb_res = [c for c in all_checks if c.category == CheckCategory.ESB]
            kv_res  = [c for c in all_checks if c.category == CheckCategory.KEY_VAULT]
            hg_res  = next((c for c in all_checks if "Hash" in c.name or "hash" in c.name), None)
            sp_res  = [c for c in all_checks if c.category == CheckCategory.SHAREPOINT]
            bd = self._conf_scorer.score(
                esb_results=esb_res, kv_results=kv_res,
                hash_result=hg_res, sp_results=sp_res,
            )
            conf_breakdown_dict = bd.to_dict()
            overall_confidence  = bd.overall

        # Determine final outcome
        fraud_flag = forgery_report_dict and forgery_report_dict.get("risk_level") == "HIGH"
        anomaly_flag = anomaly_report_dict and anomaly_report_dict.get("risk_level") == "HIGH"

        if fraud_flag:
            outcome = ValidationOutcome.FRAUD_SUSPECTED
            summary = f"FRAUD SUSPECTED: ForgeryDetector flagged HIGH risk. {'; '.join(forgery_report_dict.get('findings', [])[:2])}"
            rework  = None
        elif anomaly_flag and not all_passed:
            outcome = ValidationOutcome.ANOMALY_DETECTED
            summary = f"ANOMALY: {'; '.join(anomaly_report_dict.get('suspicious_features', [])[:2])}"
            rework  = None
        elif all_passed:
            outcome = ValidationOutcome.PASS
            summary = (
                f"All {len(all_checks)} checks PASSED. "
                f"Overall confidence: {overall_confidence:.2%}."
            )
            rework  = None
        else:
            outcome = ValidationOutcome.REWORK
            fail_names = [c.name for c in failed_checks]
            summary = (
                f"{len(failed_checks)}/{len(all_checks)} check(s) FAILED "
                f"({', '.join(fail_names)}). "
                f"Overall confidence: {overall_confidence:.2%}. "
                f"Rework triggered — SLA reset to {REWORK_SLA_HOURS} hours."
            )
            rework  = await self._rework.trigger(
                task_id=req.task_id,
                obligation_id=req.obligation_id,
                unit_head_email=req.unit_head_email,
                unit_head_name=req.unit_head_name,
                failed_checks=failed_checks,
                department=req.department,
            )

        # ── Compute canonical evidence hash (tamper-proof report seal) ────────
        canonical = json.dumps(evidence_blob, sort_keys=True, default=str).encode()
        report_hash = hashlib.sha256(canonical).hexdigest()

        report = ValidationReport(
            report_id=_new_id(),
            task_id=req.task_id,
            obligation_id=req.obligation_id,
            department=req.department,
            unit_head_email=req.unit_head_email,
            outcome=outcome,
            overall_confidence=overall_confidence,
            summary=summary,
            check_results=all_checks,
            rework=rework,
            evidence_hash=report_hash,
            validated_at=_now(),
            anomaly_report       = anomaly_report_dict,
            forgery_report       = forgery_report_dict,
            ocr_result           = ocr_result_dict,
            confidence_breakdown = conf_breakdown_dict,
        )

        logger.info(
            "Validation complete | task=%s | outcome=%s | confidence=%.4f",
            req.task_id, outcome.value, overall_confidence,
        )
        return report

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _aggregate_confidence(checks: list[CheckResult]) -> float:
        """
        Weighted harmonic mean of check confidences.
        Failed checks have weight 2× (penalise failures more than missing passes).
        """
        if not checks:
            return 0.0

        weighted_sum   = 0.0
        weight_total   = 0.0
        for c in checks:
            w = 2.0 if not c.passed else 1.0
            weighted_sum  += w / c.confidence if c.confidence > 0 else w / 0.001
            weight_total  += w

        harmonic = weight_total / weighted_sum if weighted_sum else 0.0
        return round(min(harmonic, 0.9999), 6)


# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────

def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _error_check(category: CheckCategory, name: str, exc: Exception) -> CheckResult:
    logger.exception("Check '%s' raised an exception", name)
    return CheckResult(
        check_id=_new_id(), category=category, name=name,
        passed=False, confidence=0.0,
        explanation=f"CHECK ERROR – {type(exc).__name__}: {exc}",
        evidence={"error": str(exc)},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public convenience wrapper
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_VALIDATOR: HybridValidator | None = None


def get_validator() -> HybridValidator:
    global _DEFAULT_VALIDATOR
    if _DEFAULT_VALIDATOR is None:
        _DEFAULT_VALIDATOR = HybridValidator()
    return _DEFAULT_VALIDATOR


async def validate(req: ValidationRequest) -> dict[str, Any]:
    """
    Module-level async convenience wrapper.

    Example::

        from validation.validator import validate, ValidationRequest
        report = await validate(ValidationRequest(task_id="t-001", ...))
    """
    return (await get_validator().validate(req)).to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# CLI demo
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    SAMPLE_DOC = (
        "Data Protection Policy v3.0\n"
        "Encryption: AES-256 is mandatory for all data at rest and in transit.\n"
        "Access Control: RBAC and MFA enforced across all systems.\n"
        "Incident Response: 72-hour notification window per GDPR Article 33.\n"
        "Retention Period: 7 years for financial records.\n"
        "Review Cycle: Annual review by the Chief Compliance Officer.\n"
        "Version 3.0 | Reviewed by: CCO | 2025-04-20\n"
        "ISO 27001 aligned controls applied across all domains.\n"
    ).encode()

    req = ValidationRequest(
        task_id="task-abc-123",
        obligation_id="ob-001",
        department="IT",
        unit_head_email="ciso@bank.org",
        unit_head_name="Marcus Okonkwo",
        system_id="sys-production-01",
        vault_secret="encryption-key-prod",
        document_path="/policies/DataProtectionPolicy.docx",
        document_content=SAMPLE_DOC,
        previous_doc_version=(2, 0),
        claimed_timestamp=datetime.now(tz=timezone.utc),
        run_esb_checks=True,
        run_kv_checks=True,
        run_doc_checks=True,
    )

    report = asyncio.run(get_validator().validate(req))
    print(json.dumps(report.to_dict(), indent=2))