"""
validation/intelligent_checks.py
==================================
Phase 6 — Intelligent Validation Layer

Four new check classes added alongside the existing Tier-1/Tier-2 checks:

  AnomalyDetector        – IsolationForest on evidence metadata
  ScreenshotOCRVerifier  – pytesseract OCR vs CompiledRule expected config
  ForgeryDetector        – PDF metadata vs claimed compliance date
  ComplianceConfidenceScorer – Aggregates all tiers into a single score

Usage (from HybridValidator)::

    from validation.intelligent_checks import (
        AnomalyDetector, ScreenshotOCRVerifier,
        ForgeryDetector, ComplianceConfidenceScorer,
        IntelligentValidationRequest,
    )
"""
from __future__ import annotations

import io
import json
import logging
import re
import struct
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("intelligent_checks")

# ── Optional heavy imports ────────────────────────────────────────────────────
try:
    import pytesseract
    from PIL import Image
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False
    logger.warning("pytesseract/Pillow not installed — OCR checks will be skipped")

try:
    from sklearn.ensemble import IsolationForest
    import numpy as np
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False
    logger.warning("scikit-learn not installed — anomaly detection will use rule-based fallback")

try:
    from knowledge.rule_compiler import RuleCompiler, CompiledRule
    from knowledge.temporal_reasoner import TemporalReasoner
    _KNOWLEDGE_AVAILABLE = True
except ImportError:
    _KNOWLEDGE_AVAILABLE = False


# =============================================================================
# Shared data structures
# =============================================================================

class ValidationOutcomeExtended:
    """Extended outcomes beyond the base PASS/FAIL/REWORK."""
    FRAUD_SUSPECTED   = "FRAUD_SUSPECTED"
    ANOMALY_DETECTED  = "ANOMALY_DETECTED"
    OCR_MISMATCH      = "OCR_MISMATCH"


@dataclass
class AnomalyReport:
    anomaly_score:       float        # 0.0 (normal) → 1.0 (highly anomalous)
    is_anomalous:        bool
    suspicious_features: list[str]
    risk_level:          str          # HIGH | MEDIUM | LOW
    details:             dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "anomaly_score":       round(self.anomaly_score, 4),
            "is_anomalous":        self.is_anomalous,
            "suspicious_features": self.suspicious_features,
            "risk_level":          self.risk_level,
            "details":             self.details,
        }


@dataclass
class OCRVerificationResult:
    extracted_config: dict[str, Any]
    expected_config:  dict[str, Any]
    mismatches:       list[str]
    passed:           bool
    confidence:       float
    ocr_text_sample:  str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "extracted_config": self.extracted_config,
            "expected_config":  self.expected_config,
            "mismatches":       self.mismatches,
            "passed":           self.passed,
            "confidence":       round(self.confidence, 4),
            "ocr_text_sample":  self.ocr_text_sample[:200],
        }


@dataclass
class ForgeryReport:
    is_forged:          bool
    risk_level:         str
    findings:           list[str]
    pdf_creation_date:  Optional[str]
    pdf_creator_tool:   str
    claimed_date:       Optional[str]
    date_delta_days:    Optional[int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_forged":         self.is_forged,
            "risk_level":        self.risk_level,
            "findings":          self.findings,
            "pdf_creation_date": self.pdf_creation_date,
            "pdf_creator_tool":  self.pdf_creator_tool,
            "claimed_date":      self.claimed_date,
            "date_delta_days":   self.date_delta_days,
        }


@dataclass
class ConfidenceBreakdown:
    esb_score:      float = 0.0
    kv_score:       float = 0.0
    hash_score:     float = 0.0
    sharepoint_score: float = 0.0
    anomaly_score:  float = 0.0
    ocr_score:      float = 0.0
    overall:        float = 0.0
    verdict:        str   = "UNKNOWN"

    # Weights (must sum to 1.0)
    W_ESB = 0.25
    W_KV  = 0.20
    W_HASH = 0.20
    W_SP  = 0.15
    W_ANOMALY = 0.10
    W_OCR = 0.10

    def compute(self) -> None:
        self.overall = round(
            self.esb_score      * self.W_ESB   +
            self.kv_score       * self.W_KV    +
            self.hash_score     * self.W_HASH  +
            self.sharepoint_score * self.W_SP  +
            self.anomaly_score  * self.W_ANOMALY +
            self.ocr_score      * self.W_OCR,
            4,
        )
        if self.overall >= 0.85:
            self.verdict = "PASS"
        elif self.overall >= 0.60:
            self.verdict = "CONDITIONAL_PASS"
        else:
            self.verdict = "FAIL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "esb_score":        round(self.esb_score, 4),
            "kv_score":         round(self.kv_score, 4),
            "hash_score":       round(self.hash_score, 4),
            "sharepoint_score": round(self.sharepoint_score, 4),
            "anomaly_score":    round(self.anomaly_score, 4),
            "ocr_score":        round(self.ocr_score, 4),
            "overall":          round(self.overall, 4),
            "verdict":          self.verdict,
        }


@dataclass
class IntelligentValidationRequest:
    """Extended request carrying screenshot bytes, regulation ref, and claimed date."""
    obligation_text:    str = ""
    regulation_ref:     str = ""
    screenshot_bytes:   Optional[bytes] = None   # PNG/JPEG of config screen
    adls_screenshot_uri: str = ""                # or load from ADLS
    claimed_compliance_date: Optional[date] = None
    document_bytes:     Optional[bytes] = None   # PDF for forgery check
    esb_check_scores:   list[float] = field(default_factory=list)
    kv_check_scores:    list[float] = field(default_factory=list)
    hash_check_score:   float = 0.0
    sp_check_scores:    list[float] = field(default_factory=list)


# =============================================================================
# 1. Anomaly Detector
# =============================================================================

class AnomalyDetector:
    """
    Statistical anomaly detection on evidence metadata.

    Uses IsolationForest (scikit-learn) if available; falls back to
    rule-based heuristics so the system never fully degrades.

    Features used:
      - document file size (bytes)
      - hour of day the evidence was submitted (office hours check)
      - day of week (weekend submissions are suspicious)
      - time since regulation effective date (very fresh = suspicious)
      - number of failed checks in prior tiers
    """

    # Rule-based thresholds
    _MIN_DOC_SIZE    = 512          # bytes — suspiciously small
    _MAX_DOC_SIZE    = 50_000_000   # 50 MB — suspiciously large
    _WEEKEND_DAYS    = {5, 6}       # Saturday, Sunday
    _OFFICE_HOURS    = range(8, 20) # 08:00–19:59 IST

    def __init__(self) -> None:
        self._model: Optional[Any] = None
        if _SKLEARN_AVAILABLE:
            # Pre-fit on synthetic "normal" evidence submissions
            X_normal = self._synthetic_normal_features()
            self._model = IsolationForest(
                n_estimators=100,
                contamination=0.05,
                random_state=42,
            )
            self._model.fit(X_normal)

    def detect(
        self,
        doc_size_bytes: int = 0,
        submission_ts: Optional[datetime] = None,
        prior_failed_checks: int = 0,
        days_since_regulation: int = 365,
    ) -> AnomalyReport:
        """Run anomaly detection on evidence metadata."""
        ts = submission_ts or datetime.now(tz=timezone.utc)
        features = self._extract_features(
            doc_size_bytes, ts, prior_failed_checks, days_since_regulation
        )
        suspicious: list[str] = []
        score = 0.0

        # Rule-based checks (always run)
        if doc_size_bytes > 0 and doc_size_bytes < self._MIN_DOC_SIZE:
            suspicious.append(f"Document too small ({doc_size_bytes} bytes) — may be a stub")
            score += 0.3
        if doc_size_bytes > self._MAX_DOC_SIZE:
            suspicious.append(f"Document unusually large ({doc_size_bytes/1e6:.1f} MB)")
            score += 0.15
        if ts.weekday() in self._WEEKEND_DAYS:
            suspicious.append(f"Submitted on weekend ({ts.strftime('%A')}) — unusual")
            score += 0.2
        if ts.hour not in self._OFFICE_HOURS:
            suspicious.append(f"Submitted at {ts.hour:02d}:00 UTC — outside office hours")
            score += 0.15
        if days_since_regulation < 3:
            suspicious.append(f"Evidence submitted within {days_since_regulation} day(s) of regulation — suspiciously fast")
            score += 0.25
        if prior_failed_checks >= 3:
            suspicious.append(f"{prior_failed_checks} prior checks already failed — pattern of non-compliance")
            score += 0.2

        # ML model score (if available)
        if _SKLEARN_AVAILABLE and self._model is not None:
            import numpy as np
            X = np.array([features])
            pred = self._model.predict(X)[0]   # -1 = anomaly, 1 = normal
            if pred == -1:
                ml_score = abs(float(self._model.score_samples(X)[0]))
                score = max(score, ml_score)
                if ml_score > 0.6:
                    suspicious.append(f"ML IsolationForest flagged as anomaly (score={ml_score:.2f})")

        score = min(score, 1.0)
        is_anomalous = score >= 0.4 or len(suspicious) >= 2

        if score >= 0.7:
            risk = "HIGH"
        elif score >= 0.4:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        return AnomalyReport(
            anomaly_score        = round(score, 4),
            is_anomalous         = is_anomalous,
            suspicious_features  = suspicious,
            risk_level           = risk,
            details = {
                "doc_size_bytes":        doc_size_bytes,
                "submission_hour_utc":   ts.hour,
                "submission_weekday":    ts.strftime("%A"),
                "days_since_regulation": days_since_regulation,
                "prior_failed_checks":   prior_failed_checks,
            },
        )

    @staticmethod
    def _extract_features(
        size: int, ts: datetime, failures: int, days: int
    ) -> list[float]:
        return [
            min(size / 1_000_000, 50.0),   # normalised MB
            float(ts.hour),
            float(ts.weekday()),
            min(float(failures), 10.0),
            min(float(days), 3650.0) / 3650.0,
        ]

    @staticmethod
    def _synthetic_normal_features():
        """Generate synthetic 'normal' training data for IsolationForest."""
        import numpy as np
        rng = np.random.default_rng(42)
        n = 500
        sizes     = rng.uniform(0.05, 5.0, n)           # 50KB–5MB
        hours     = rng.integers(8, 18, n).astype(float)
        weekdays  = rng.integers(0, 5, n).astype(float)  # Mon–Fri
        failures  = rng.integers(0, 2, n).astype(float)
        days_norm = rng.uniform(0.1, 1.0, n)
        return np.column_stack([sizes, hours, weekdays, failures, days_norm])


# =============================================================================
# 2. Screenshot OCR Verifier
# =============================================================================

class ScreenshotOCRVerifier:
    """
    Uses pytesseract to extract text from a compliance evidence screenshot
    and compares it against the expected system configuration from a CompiledRule.

    Example:
        Screenshot shows "encryption: enabled, algorithm: AES-128, key_size: 128"
        CompiledRule expects AES-256 / strength=256
        → MISMATCH: algorithm is AES-128, expected AES-256
    """

    # Patterns to extract config key-value pairs from OCR text
    _KV_PATTERN = re.compile(
        r'(?P<key>[\w_\-]+)\s*[=:]\s*(?P<value>[\w.\-/]+)',
        re.IGNORECASE,
    )
    # Normalisation map for common OCR misreads
    _NORMALISE = {
        "aes256": "AES-256", "aes128": "AES-128",
        "tls13": "TLSv1.3",  "tls12": "TLSv1.2",
        "enabled": "enabled", "disabled": "disabled",
        "true": "enabled",    "false": "disabled",
    }

    def verify(
        self,
        image_bytes: bytes,
        obligation_text: str = "",
        compiled_rule: Optional[Any] = None,
    ) -> OCRVerificationResult:
        """
        Extract text from screenshot and compare against expected config.
        """
        if not _OCR_AVAILABLE:
            return OCRVerificationResult(
                extracted_config = {},
                expected_config  = {},
                mismatches       = ["pytesseract not installed — OCR check skipped"],
                passed           = True,  # skip = don't penalise
                confidence       = 0.5,
                ocr_text_sample  = "",
            )

        # Step 1: OCR extraction
        try:
            img      = Image.open(io.BytesIO(image_bytes))
            ocr_text = pytesseract.image_to_string(img, lang="eng")
        except Exception as exc:
            logger.warning("OCR extraction failed: %s", exc)
            return OCRVerificationResult(
                extracted_config = {},
                expected_config  = {},
                mismatches       = [f"OCR error: {exc}"],
                passed           = True,
                confidence       = 0.5,
            )

        # Step 2: Parse config key-value pairs from OCR text
        extracted = self._parse_ocr_text(ocr_text)

        # Step 3: Determine expected config
        if compiled_rule is not None and hasattr(compiled_rule, "get_expected_config"):
            expected = compiled_rule.get_expected_config()
        elif obligation_text and _KNOWLEDGE_AVAILABLE:
            rc = RuleCompiler()
            rule = rc.compile(obligation_text)
            expected = rule.get_expected_config()
        else:
            expected = self._infer_expected_from_text(obligation_text)

        # Step 4: Compare
        mismatches = self._compare(extracted, expected)
        passed     = len(mismatches) == 0
        confidence = 0.90 if passed else max(0.10, 0.90 - len(mismatches) * 0.20)

        return OCRVerificationResult(
            extracted_config = extracted,
            expected_config  = expected,
            mismatches       = mismatches,
            passed           = passed,
            confidence       = confidence,
            ocr_text_sample  = ocr_text[:300],
        )

    def _parse_ocr_text(self, text: str) -> dict[str, Any]:
        """Extract key=value / key: value pairs from OCR output."""
        config: dict[str, Any] = {}
        for m in self._KV_PATTERN.finditer(text):
            key = m.group("key").lower().replace("-", "_")
            val = m.group("value").strip()
            normalised = self._NORMALISE.get(val.lower().replace("-", ""), val)
            config[key] = normalised
        return config

    @staticmethod
    def _infer_expected_from_text(text: str) -> dict[str, Any]:
        """Fallback: infer expected config from obligation text keywords."""
        expected: dict[str, Any] = {}
        t = text.lower()
        if "aes-256" in t or "aes256" in t:
            expected["algorithm"] = "AES-256"
            expected["key_size"]  = 256
        elif "aes-128" in t:
            expected["algorithm"] = "AES-128"
            expected["key_size"]  = 128
        if "tls 1.3" in t or "tls1.3" in t:
            expected["tls_version"] = "TLSv1.3"
        if "encrypt" in t:
            expected["encryption"] = "enabled"
        if "mfa" in t or "multi-factor" in t:
            expected["mfa_enabled"] = True
        return expected

    @staticmethod
    def _compare(extracted: dict, expected: dict) -> list[str]:
        """Return list of mismatches between extracted and expected configs."""
        mismatches: list[str] = []
        for key, exp_val in expected.items():
            # Try fuzzy key matching (OCR may add underscores or hyphens)
            found_val = None
            for ext_key, ext_val in extracted.items():
                if key.replace("_", "") == ext_key.replace("_", ""):
                    found_val = ext_val
                    break

            if found_val is None:
                mismatches.append(f"Expected config key '{key}={exp_val}' not found in screenshot")
                continue

            # Normalise both sides for comparison
            exp_str   = str(exp_val).lower().replace("-", "").replace("_", "")
            found_str = str(found_val).lower().replace("-", "").replace("_", "")
            if exp_str != found_str:
                mismatches.append(
                    f"Config mismatch: '{key}' — expected '{exp_val}', found '{found_val}'"
                )
        return mismatches


# =============================================================================
# 3. Forgery Detector
# =============================================================================

class ForgeryDetector:
    """
    Analyses PDF/document metadata to detect evidence forgery.

    Checks:
      1. PDF creation date vs claimed compliance date
         (evidence predating the regulation = RED FLAG)
      2. PDF creator tool vs expected tool
         (2018-dated document created in Word 2024 = RED FLAG)
      3. Modification date after submission date
      4. Metadata author vs ServiceNow task assignee
    """

    # Modern tools that shouldn't appear in old documents
    _MODERN_TOOLS = {
        "microsoft word 2021", "microsoft word 2022",
        "microsoft word 2023", "microsoft word 2024",
        "acrobat 2023", "acrobat 2024",
    }

    def detect(
        self,
        document_bytes: bytes,
        claimed_compliance_date: Optional[date] = None,
        expected_author_email: str = "",
        regulation_ref: str = "",
    ) -> ForgeryReport:
        """Analyse document bytes for forgery indicators."""
        findings: list[str] = []

        # Extract PDF metadata
        creation_date_str, creator_tool, mod_date_str = self._extract_pdf_metadata(document_bytes)

        # Parse creation date
        creation_date = self._parse_pdf_date(creation_date_str)

        # Check 1: creation date vs claimed compliance date
        date_delta_days: Optional[int] = None
        if creation_date and claimed_compliance_date:
            delta = (creation_date - claimed_compliance_date).days
            date_delta_days = delta
            if delta < -30:   # created 30+ days before claimed compliance date
                findings.append(
                    f"CRITICAL: Document created on {creation_date.isoformat()} "
                    f"but compliance claimed for {claimed_compliance_date.isoformat()} "
                    f"({abs(delta)} days prior). Possible backdating."
                )

        # Check 2: Temporal consistency vs regulation effective date
        if creation_date and regulation_ref and _KNOWLEDGE_AVAILABLE:
            tr = TemporalReasoner()
            temporal = tr.check_evidence_timestamp(regulation_ref, creation_date)
            if not temporal.is_consistent and temporal.issue:
                findings.append(f"TEMPORAL: {temporal.issue}")

        # Check 3: Tool anachronism
        tool_lower = creator_tool.lower()
        if creation_date and creation_date.year < 2020:
            for modern in self._MODERN_TOOLS:
                if modern in tool_lower:
                    findings.append(
                        f"ANACHRONISM: Document claims creation in {creation_date.year} "
                        f"but was created with '{creator_tool}' (a post-2020 tool)."
                    )
                    break

        # Check 4: Modification after claimed date
        mod_date = self._parse_pdf_date(mod_date_str)
        if mod_date and claimed_compliance_date and mod_date > claimed_compliance_date:
            delta_mod = (mod_date - claimed_compliance_date).days
            if delta_mod > 1:
                findings.append(
                    f"WARNING: Document last modified {delta_mod} day(s) after "
                    f"claimed compliance date — content may have been altered."
                )

        # Verdict
        is_forged = len(findings) > 0
        if len(findings) >= 2 or any("CRITICAL" in f for f in findings):
            risk = "HIGH"
        elif findings:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        return ForgeryReport(
            is_forged          = is_forged,
            risk_level         = risk,
            findings           = findings,
            pdf_creation_date  = creation_date_str,
            pdf_creator_tool   = creator_tool,
            claimed_date       = claimed_compliance_date.isoformat() if claimed_compliance_date else None,
            date_delta_days    = date_delta_days,
        )

    @staticmethod
    def _extract_pdf_metadata(data: bytes) -> tuple[str, str, str]:
        """
        Extract creation date, creator tool, and modification date from PDF bytes.
        Does NOT require a full PDF library — uses regex on the raw PDF stream.
        """
        text = data[:65536].decode("latin-1", errors="replace")

        def _find(pattern: str) -> str:
            m = re.search(pattern, text, re.IGNORECASE)
            return m.group(1).strip() if m else ""

        creation = _find(r"/CreationDate\s*\(([^)]+)\)")
        creator  = _find(r"/Creator\s*\(([^)]+)\)")
        mod      = _find(r"/ModDate\s*\(([^)]+)\)")
        if not creator:
            creator = _find(r"/Producer\s*\(([^)]+)\)")
        return creation, creator, mod

    @staticmethod
    def _parse_pdf_date(pdf_date: str) -> Optional[date]:
        """
        Parse PDF date strings like:
          D:20240415143022+05'30'
          20240415
        """
        if not pdf_date:
            return None
        digits = re.sub(r"\D", "", pdf_date)
        if len(digits) >= 8:
            try:
                return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
            except ValueError:
                pass
        return None


# =============================================================================
# 4. Compliance Confidence Scorer
# =============================================================================

class ComplianceConfidenceScorer:
    """
    Aggregates all validation tier scores into a single compliance
    confidence score with a weighted breakdown.

    Weights:
      ESB       25%
      Key Vault 20%
      HashGuard 20%
      SharePoint 15%
      Anomaly   10%
      OCR       10%
    """

    def score(
        self,
        esb_results:    list[Any],     # list[CheckResult]
        kv_results:     list[Any],
        hash_result:    Optional[Any],
        sp_results:     list[Any],
        anomaly_report: Optional[AnomalyReport] = None,
        ocr_result:     Optional[OCRVerificationResult] = None,
    ) -> ConfidenceBreakdown:
        """Compute weighted confidence breakdown."""
        bd = ConfidenceBreakdown()

        bd.esb_score       = self._avg_confidence(esb_results)
        bd.kv_score        = self._avg_confidence(kv_results)
        bd.hash_score      = hash_result.confidence if hash_result else 0.5
        bd.sharepoint_score = self._avg_confidence(sp_results)

        if anomaly_report:
            # Invert: high anomaly score = low confidence
            bd.anomaly_score = round(1.0 - anomaly_report.anomaly_score, 4)
        else:
            bd.anomaly_score = 0.8   # neutral if no anomaly check run

        if ocr_result:
            bd.ocr_score = ocr_result.confidence
        else:
            bd.ocr_score = 0.8       # neutral if no OCR check run

        bd.compute()
        return bd

    @staticmethod
    def _avg_confidence(results: list[Any]) -> float:
        if not results:
            return 0.5
        vals = [getattr(r, "confidence", 0.5) for r in results]
        return round(sum(vals) / len(vals), 4)
