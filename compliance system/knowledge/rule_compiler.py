"""
knowledge/rule_compiler.py
===========================
Converts regulatory obligation text into typed, machine-executable CompiledRule objects.

This is the core of the domain-specific intelligence — replacing generic LLM
classification with a structured rule extraction pipeline that produces
deterministic, auditable output.

A CompiledRule captures:
  subject          : who the obligation applies to
  verb             : what action is required
  object           : what the action applies to
  qualifier        : how/when/where the action must be done
  algorithm        : specific technical standard (e.g. AES-256, TLS 1.3)
  strength         : numeric strength (e.g. 256 for AES-256, 1.3 for TLS 1.3)
  mandatory        : True if "shall/must", False if "should/may"
  deadline_days    : inferred deadline (from risk level and regulation type)
  evidence_type    : what proof is needed
  domain           : compliance domain
  confidence       : compiler confidence (0.0–1.0)

Usage::

    compiler = RuleCompiler()
    rule = compiler.compile(
        "Banks shall encrypt all customer data at rest using AES-256 encryption."
    )
    # → CompiledRule(subject='Banks', verb='encrypt', object='customer data',
    #                qualifier='at rest', algorithm='AES-256', strength=256,
    #                mandatory=True, evidence_type='system_config', domain='encryption')
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class CompiledRule:
    """A machine-executable representation of a regulatory obligation."""
    raw_text:       str
    subject:        str         = ""       # "Banks", "NBFCs", "Data Fiduciaries"
    verb:           str         = ""       # "encrypt", "report", "retain", "implement"
    object:         str         = ""       # "customer data", "incidents", "logs"
    qualifier:      str         = ""       # "at rest", "within 6 hours", "for 5 years"
    algorithm:      str         = ""       # "AES-256", "TLS 1.3", "SHA-256"
    strength:       float       = 0.0     # numeric strength (256, 1.3, 5)
    unit:           str         = ""       # "bits", "years", "days", "hours"
    mandatory:      bool        = True
    deadline_days:  int         = 30
    evidence_type:  str         = "compliance_certificate"
    domain:         str         = "cyber"
    confidence:     float       = 0.0
    regulation_refs: list[str]  = field(default_factory=list)
    keywords:       list[str]   = field(default_factory=list)
    validation_checks: list[str] = field(default_factory=list)  # what ESB/KV checks to run

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_text":         self.raw_text[:200],
            "subject":          self.subject,
            "verb":             self.verb,
            "object":           self.object,
            "qualifier":        self.qualifier,
            "algorithm":        self.algorithm,
            "strength":         self.strength,
            "unit":             self.unit,
            "mandatory":        self.mandatory,
            "deadline_days":    self.deadline_days,
            "evidence_type":    self.evidence_type,
            "domain":           self.domain,
            "confidence":       round(self.confidence, 4),
            "regulation_refs":  self.regulation_refs,
            "keywords":         self.keywords,
            "validation_checks": self.validation_checks,
        }

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.80

    def get_expected_config(self) -> dict[str, Any]:
        """
        Return the expected system configuration that proves compliance.
        Used by ScreenshotOCRVerifier to compare against actual screenshots.
        """
        config: dict[str, Any] = {}
        if self.algorithm:
            config["algorithm"] = self.algorithm
        if self.strength > 0:
            config["key_size"] = int(self.strength) if self.unit in ("bits", "") else self.strength
            config["strength"] = self.strength
            config["unit"] = self.unit
        if "encrypt" in self.verb.lower():
            config["encryption"] = "enabled"
            config["encryption_standard"] = self.algorithm
        if "tls" in self.algorithm.lower():
            config["tls_version"] = self.algorithm  # e.g. "TLSv1.3"
        if "mfa" in self.object.lower() or "mfa" in self.qualifier.lower():
            config["mfa_enabled"] = True
        return config


@dataclass
class RuleCompilationResult:
    """Result of compiling an obligation text (may produce multiple rules)."""
    original_text: str
    rules:         list[CompiledRule] = field(default_factory=list)
    unresolved:    list[str] = field(default_factory=list)  # parts not compiled
    overall_confidence: float = 0.0
    notes:         list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_text":      self.original_text[:300],
            "rule_count":         len(self.rules),
            "overall_confidence": round(self.overall_confidence, 4),
            "rules":              [r.to_dict() for r in self.rules],
            "unresolved":         self.unresolved,
            "notes":              self.notes,
        }


# =============================================================================
# Pattern Libraries
# =============================================================================

# Regulatory subjects (who the obligation applies to)
_SUBJECT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(?:all\s+)?(?:scheduled\s+commercial\s+)?banks?\b', re.I), "Banks"),
    (re.compile(r'\bnbf[cs]?\b', re.I), "NBFCs"),
    (re.compile(r'\bpayment\s+banks?\b', re.I), "Payment Banks"),
    (re.compile(r'\bdata\s+fiduciar(?:y|ies)\b', re.I), "Data Fiduciaries"),
    (re.compile(r'\bservice\s+providers?\b', re.I), "Service Providers"),
    (re.compile(r'\bmarket\s+infrastructure\s+institutions?\b', re.I), "Market Infrastructure Institutions"),
    (re.compile(r'\bsebi[-\s]?regulated\s+entit(?:y|ies)\b', re.I), "SEBI Regulated Entities"),
    (re.compile(r'\bregulated\s+entit(?:y|ies)\b', re.I), "Regulated Entities"),
    (re.compile(r'\borganisations?\b|\borganizations?\b', re.I), "Organisations"),
    (re.compile(r'\bintermediaries?\b', re.I), "Intermediaries"),
    (re.compile(r'\bstock\s+exchanges?\b', re.I), "Stock Exchanges"),
    (re.compile(r'\ball\s+entit(?:y|ies)\b', re.I), "All Entities"),
]

# Mandatory / permissive modal verbs
_MANDATORY_PATTERNS = re.compile(
    r'\b(?:shall|must|required\s+to|obligated\s+to|mandatory|is\s+required|'
    r'are\s+required|needs?\s+to|directed\s+to|should\s+compulsorily)\b',
    re.I,
)
_PERMISSIVE_PATTERNS = re.compile(
    r'\b(?:should|may|can|could|recommended|advisable|is\s+encouraged)\b',
    re.I,
)

# Action verbs (what must be done)
_VERB_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # (pattern, canonical_verb, domain)
    (re.compile(r'\bencrypt\w*\b', re.I),              "encrypt",    "encryption"),
    (re.compile(r'\breport\w*\b', re.I),               "report",     "cyber"),
    (re.compile(r'\bretain\w*\b|\bpreserve\w*\b|\bkeep\b', re.I), "retain", "audit"),
    (re.compile(r'\bimplements?\b|\bdeploy\w*\b', re.I), "implement", "cyber"),
    (re.compile(r'\bmaintain\w*\b', re.I),             "maintain",   "audit"),
    (re.compile(r'\bconduct\w*\b|\bperform\w*\b', re.I), "conduct",  "audit"),
    (re.compile(r'\bmonitor\w*\b', re.I),              "monitor",    "fraud"),
    (re.compile(r'\bnotif(?:y|ies|ied)\b', re.I),     "notify",     "cyber"),
    (re.compile(r'\berases?\b|\bdelete\w*\b|\bpurg\w*\b', re.I), "erase", "data_protection"),
    (re.compile(r'\bverif(?:y|ies|ied)\b', re.I),     "verify",     "kyc"),
    (re.compile(r'\bscreen\w*\b', re.I),               "screen",     "aml"),
    (re.compile(r'\bsegment\w*\b', re.I),              "segment",    "cyber"),
    (re.compile(r'\bpatch\w*\b', re.I),                "patch",      "cyber"),
    (re.compile(r'\btest\w*\b', re.I),                 "test",       "audit"),
    (re.compile(r'\brotate\w*\b', re.I),               "rotate",     "encryption"),
    (re.compile(r'\bbackup\w*\b', re.I),               "backup",     "cyber"),
    (re.compile(r'\bescalate\w*\b', re.I),             "escalate",   "aml"),
]

# Technical algorithms / standards
_ALGORITHM_PATTERNS: list[tuple[re.Pattern, str, float, str]] = [
    # (pattern, canonical_name, strength, unit)
    (re.compile(r'\baes[-\s]?256\b', re.I),            "AES-256",    256.0,  "bits"),
    (re.compile(r'\baes[-\s]?128\b', re.I),            "AES-128",    128.0,  "bits"),
    (re.compile(r'\btls[-\s]?1\.?3\b', re.I),          "TLSv1.3",    1.3,    "version"),
    (re.compile(r'\btls[-\s]?1\.?2\b', re.I),          "TLSv1.2",    1.2,    "version"),
    (re.compile(r'\brsa[-\s]?4096\b', re.I),           "RSA-4096",   4096.0, "bits"),
    (re.compile(r'\brsa[-\s]?2048\b', re.I),           "RSA-2048",   2048.0, "bits"),
    (re.compile(r'\bsha[-\s]?256\b', re.I),            "SHA-256",    256.0,  "bits"),
    (re.compile(r'\bsha[-\s]?512\b', re.I),            "SHA-512",    512.0,  "bits"),
    (re.compile(r'\bhmac\b', re.I),                    "HMAC",       0.0,    ""),
    (re.compile(r'\becdsa\b', re.I),                   "ECDSA",      0.0,    ""),
    (re.compile(r'\bfido2?\b|\bwebauthn\b', re.I),     "FIDO2",      0.0,    ""),
    (re.compile(r'\bmfa\b|\bmulti.?factor\b', re.I),   "MFA",        0.0,    ""),
    (re.compile(r'\biso\s*27001\b', re.I),             "ISO-27001",  0.0,    ""),
    (re.compile(r'\bpci.?dss\b', re.I),                "PCI-DSS",    0.0,    ""),
    (re.compile(r'\bvapt\b', re.I),                    "VAPT",       0.0,    ""),
    (re.compile(r'\bsiem\b', re.I),                    "SIEM",       0.0,    ""),
]

# Objects (what the action is performed on)
_OBJECT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bcustomer\s+data\b', re.I),            "customer data"),
    (re.compile(r'\bpersonal\s+(?:data|information)\b', re.I), "personal data"),
    (re.compile(r'\btransaction\s+(?:data|records?|logs?)\b', re.I), "transaction data"),
    (re.compile(r'\bict\s+(?:system\s+)?logs?\b', re.I), "ICT system logs"),
    (re.compile(r'\bvpn\s+logs?\b', re.I),               "VPN logs"),
    (re.compile(r'\bsystem\s+logs?\b', re.I),            "system logs"),
    (re.compile(r'\bkyc\s+(?:data|records?)\b', re.I),   "KYC records"),
    (re.compile(r'\bincidents?\b', re.I),                 "cyber incidents"),
    (re.compile(r'\bsuspicious\s+transactions?\b', re.I), "suspicious transactions"),
    (re.compile(r'\bsensitive\s+(?:data|information)\b', re.I), "sensitive data"),
    (re.compile(r'\bpayment\s+data\b', re.I),             "payment data"),
    (re.compile(r'\bcard\s+data\b', re.I),                "card data"),
    (re.compile(r'\bbackups?\b', re.I),                   "backup data"),
    (re.compile(r'\bnetwork\s+traffic\b', re.I),          "network traffic"),
    (re.compile(r'\bsource\s+code\b', re.I),              "source code"),
    (re.compile(r'\bapplication\s+logs?\b', re.I),        "application logs"),
]

# Qualifiers (how/when/where)
_QUALIFIER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bat\s+rest\b', re.I),                  "at rest"),
    (re.compile(r'\bin\s+transit\b|\bin\s+motion\b', re.I), "in transit"),
    (re.compile(r'\bat\s+rest\s+and\s+in\s+transit\b', re.I), "at rest and in transit"),
    (re.compile(r'\bwithin\s+6\s+hours?\b', re.I),        "within 6 hours"),
    (re.compile(r'\bwithin\s+24\s+hours?\b', re.I),       "within 24 hours"),
    (re.compile(r'\bwithin\s+72\s+hours?\b', re.I),       "within 72 hours"),
    (re.compile(r'\bwithin\s+7\s+days?\b', re.I),         "within 7 days"),
    (re.compile(r'\bwithin\s+30\s+days?\b', re.I),        "within 30 days"),
    (re.compile(r'\bfor\s+5\s+years?\b', re.I),           "for 5 years"),
    (re.compile(r'\bfor\s+180\s+days?\b', re.I),          "for 180 days"),
    (re.compile(r'\bevery\s+6\s+months?\b', re.I),        "every 6 months"),
    (re.compile(r'\bannually\b|\bevery\s+year\b', re.I),  "annually"),
    (re.compile(r'\b24x7\b|\b24\/7\b|\bround.the.clock\b', re.I), "24x7"),
    (re.compile(r'\breal.?time\b', re.I),                 "real-time"),
    (re.compile(r'\bin\s+india\b|\bwithin\s+india\b', re.I), "in India"),
    (re.compile(r'\bon\s+(?:the\s+)?cloud\b', re.I),      "on cloud"),
]

# Deadline extraction (number of days)
_DEADLINE_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r'\bwithin\s+6\s+hours?\b', re.I),    1),    # treat as urgent = 1 day
    (re.compile(r'\bwithin\s+24\s+hours?\b', re.I),   1),
    (re.compile(r'\bwithin\s+48\s+hours?\b', re.I),   2),
    (re.compile(r'\bwithin\s+72\s+hours?\b', re.I),   3),
    (re.compile(r'\bwithin\s+7\s+days?\b', re.I),     7),
    (re.compile(r'\bwithin\s+14\s+days?\b', re.I),    14),
    (re.compile(r'\bwithin\s+30\s+days?\b', re.I),    30),
    (re.compile(r'\bwithin\s+60\s+days?\b', re.I),    60),
    (re.compile(r'\bwithin\s+90\s+days?\b', re.I),    90),
    (re.compile(r'\beverys?\s+6\s+months?\b', re.I),  180),
    (re.compile(r'\bannually\b|\beverys?\s+year\b', re.I), 365),
]

# Evidence type inference
_EVIDENCE_RULES: list[tuple[list[str], str]] = [
    (["encrypt", "tls", "ssl", "algorithm"],     "system_config"),
    (["vapt", "penetration", "vulnerability"],   "vapt_report"),
    (["audit", "review", "inspect"],             "audit_report"),
    (["policy", "procedure", "framework"],       "policy_document"),
    (["report", "notify", "file", "submit"],     "regulatory_filing"),
    (["retain", "maintain", "preserve", "keep"], "retention_log"),
    (["monitor", "detect", "alert"],             "monitoring_log"),
    (["train", "awareness", "educate"],          "training_record"),
    (["backup", "restore", "recovery"],          "backup_verification"),
    (["screen", "kyc", "verify", "identity"],    "kyc_document"),
]

# Regulation reference extraction
_REG_REF_PATTERN = re.compile(
    r'(?:RBI|SEBI|CERT-In|CERT\.?In|DPDP|IRDAI|PFRDA)'
    r'[/\s]*(?:circular|notification|master\s+direction|guideline|'
    r'master\s+circular|direction|advisory|act)?'
    r'[/\s-]*[\d\w\-/]+',
    re.IGNORECASE,
)


# =============================================================================
# Rule Compiler
# =============================================================================

class RuleCompiler:
    """
    Converts raw regulatory obligation text into structured CompiledRule objects.

    The compiler runs a cascade of pattern matching steps:
      1. Extract regulation references
      2. Determine mandatory vs permissive
      3. Extract subject
      4. Extract verb and infer domain
      5. Extract object
      6. Extract qualifier
      7. Extract algorithm and technical strength
      8. Infer deadline and evidence type
      9. Score confidence

    Usage::

        compiler = RuleCompiler()
        result = compiler.compile_text("Banks shall encrypt customer data at rest using AES-256.")
        for rule in result.rules:
            print(rule.algorithm, rule.mandatory, rule.evidence_type)
    """

    def compile(self, obligation_text: str) -> CompiledRule:
        """
        Compile a single obligation sentence into a CompiledRule.
        Returns a CompiledRule with confidence score.
        """
        text = obligation_text.strip()
        if not text:
            return CompiledRule(raw_text=text, confidence=0.0)

        rule = CompiledRule(raw_text=text)
        confidence_components: list[float] = []

        # Step 1: Regulation references
        rule.regulation_refs = self._extract_reg_refs(text)

        # Step 2: Mandatory vs permissive
        if _MANDATORY_PATTERNS.search(text):
            rule.mandatory = True
            confidence_components.append(0.15)
        elif _PERMISSIVE_PATTERNS.search(text):
            rule.mandatory = False
            confidence_components.append(0.05)
        else:
            rule.mandatory = True  # default assumption for regulatory text
            confidence_components.append(0.05)

        # Step 3: Subject extraction
        subject, subj_conf = self._extract_subject(text)
        rule.subject = subject
        confidence_components.append(subj_conf)

        # Step 4: Verb and domain
        verb, domain, verb_conf = self._extract_verb_and_domain(text)
        rule.verb = verb
        rule.domain = domain
        confidence_components.append(verb_conf)

        # Step 5: Object extraction
        obj, obj_conf = self._extract_object(text)
        rule.object = obj
        confidence_components.append(obj_conf)

        # Step 6: Qualifier
        qualifier, _ = self._extract_qualifier(text)
        rule.qualifier = qualifier

        # Step 7: Algorithm + strength
        algorithm, strength, unit, algo_conf = self._extract_algorithm(text)
        rule.algorithm = algorithm
        rule.strength = strength
        rule.unit = unit
        confidence_components.append(algo_conf)

        # Step 8: Deadline inference
        rule.deadline_days = self._infer_deadline(text, rule.mandatory)

        # Step 9: Evidence type
        rule.evidence_type = self._infer_evidence_type(
            f"{rule.verb} {rule.object} {rule.qualifier}".lower()
        )

        # Step 10: Validation checks to schedule
        rule.validation_checks = self._infer_validation_checks(rule)

        # Step 11: Keywords
        rule.keywords = self._extract_keywords(text)

        # Confidence = weighted average of components
        if confidence_components:
            rule.confidence = min(sum(confidence_components) / len(confidence_components), 1.0)

        return rule

    def compile_text(self, obligation_text: str) -> RuleCompilationResult:
        """
        Split text into sentences and compile each into a rule.
        Returns a RuleCompilationResult with all extracted rules.
        """
        sentences = self._split_sentences(obligation_text)
        result = RuleCompilationResult(original_text=obligation_text)
        confidences: list[float] = []

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 15:
                continue
            rule = self.compile(sentence)
            if rule.confidence > 0.2:
                result.rules.append(rule)
                confidences.append(rule.confidence)
            else:
                result.unresolved.append(sentence[:100])

        if confidences:
            result.overall_confidence = sum(confidences) / len(confidences)
        if not result.rules:
            result.notes.append("No rules compiled above 0.2 confidence threshold")

        return result

    # ── Extraction Helpers ────────────────────────────────────────────────────

    def _extract_reg_refs(self, text: str) -> list[str]:
        """Extract official regulatory reference strings."""
        return [m.group().strip() for m in _REG_REF_PATTERN.finditer(text)]

    def _extract_subject(self, text: str) -> tuple[str, float]:
        for pattern, subject in _SUBJECT_PATTERNS:
            if pattern.search(text):
                return subject, 0.20
        # Implicit subject
        if re.search(r'\borganisations?\b|\bcompan(?:y|ies)\b|\bentit(?:y|ies)\b', text, re.I):
            return "Regulated Entities", 0.10
        return "Regulated Entities", 0.05  # default fallback

    def _extract_verb_and_domain(self, text: str) -> tuple[str, str, float]:
        for pattern, verb, domain in _VERB_PATTERNS:
            if pattern.search(text):
                return verb, domain, 0.20
        return "comply", "cyber", 0.05

    def _extract_object(self, text: str) -> tuple[str, float]:
        for pattern, obj in _OBJECT_PATTERNS:
            if pattern.search(text):
                return obj, 0.15
        return "systems", 0.05

    def _extract_qualifier(self, text: str) -> tuple[str, float]:
        # Check combined qualifier first
        combined = re.search(r'\bat\s+rest\s+and\s+in\s+transit\b', text, re.I)
        if combined:
            return "at rest and in transit", 0.15

        for pattern, qualifier in _QUALIFIER_PATTERNS:
            if pattern.search(text):
                return qualifier, 0.10
        return "", 0.0

    def _extract_algorithm(self, text: str) -> tuple[str, float, str, float]:
        """Return (algorithm, strength, unit, confidence)."""
        for pattern, algo, strength, unit in _ALGORITHM_PATTERNS:
            if pattern.search(text):
                return algo, strength, unit, 0.20
        return "", 0.0, "", 0.0

    def _infer_deadline(self, text: str, mandatory: bool) -> int:
        """Infer deadline in days from the obligation text."""
        for pattern, days in _DEADLINE_PATTERNS:
            if pattern.search(text):
                return days
        # Defaults based on mandatory flag
        return 14 if mandatory else 60

    def _infer_evidence_type(self, combined_text: str) -> str:
        """Map verb+object+qualifier → evidence type."""
        for keywords, evidence_type in _EVIDENCE_RULES:
            if any(kw in combined_text for kw in keywords):
                return evidence_type
        return "compliance_certificate"

    def _infer_validation_checks(self, rule: CompiledRule) -> list[str]:
        """
        Determine which ESB / Key Vault checks should be automatically scheduled.
        Output maps to the validation layer check names.
        """
        checks: list[str] = []
        if "AES" in rule.algorithm or "encrypt" in rule.verb.lower():
            checks.append("ESB:encryption_check")
        if "TLS" in rule.algorithm:
            checks.append("ESB:tls_check")
        if "MFA" in rule.algorithm or "access" in rule.object.lower():
            checks.append("ESB:access_control_check")
        if rule.algorithm in ("AES-256", "RSA-4096"):
            checks.append("KV:key_policy_check")
            checks.append("KV:rotation_schedule_check")
        if "retain" in rule.verb or "log" in rule.object.lower():
            checks.append("SP:version_increment_check")
        if "policy" in rule.evidence_type or "document" in rule.evidence_type:
            checks.append("SP:keyword_scan_check")
        return checks

    def _extract_keywords(self, text: str) -> list[str]:
        """Extract compliance domain keywords from text."""
        kw_pattern = re.compile(
            r'\b(?:KYC|AML|CFT|encryption|AES|TLS|SSL|audit|compliance|'
            r'data\s+protection|DPDP|GDPR|cyber|NPA|capital|liquidity|'
            r'risk|fraud|outsourcing|VAPT|SIEM|SOC|MFA|RBAC|'
            r'tokenisation|incident|reporting|retention|backup|patch|'
            r'PEP|STR|CTR|CRAR|LCR|CET1)\b',
            re.IGNORECASE,
        )
        return list(set(m.group().upper() for m in kw_pattern.finditer(text)))

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split text into sentences, respecting regulatory list items."""
        # Split on period followed by space and capital letter, or newlines
        sentences = re.split(r'(?<=[.;])\s+(?=[A-Z])', text)
        # Also split on bullet-style numbering
        result: list[str] = []
        for s in sentences:
            sub = re.split(r'\n\s*(?:\d+\.|\-|\•)\s+', s)
            result.extend(sub)
        return [s.strip() for s in result if s.strip()]


# =============================================================================
# Convenience Functions
# =============================================================================

def compile_obligation(text: str) -> CompiledRule:
    """Convenience function: compile a single obligation sentence."""
    return RuleCompiler().compile(text)


def compile_obligation_batch(texts: list[str]) -> list[CompiledRule]:
    """Compile a batch of obligation sentences."""
    compiler = RuleCompiler()
    return [compiler.compile(text) for text in texts]
