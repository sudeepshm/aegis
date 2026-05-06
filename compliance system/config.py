"""
config.py
=========
Centralised, secure configuration for the compliance automation platform.

Design principles
-----------------
* All secrets are pulled from Azure Key Vault at startup — nothing is
  hard-coded or read from plain environment variables for sensitive values.
* Non-sensitive structural settings (region, container names, log level)
  may be supplied via environment variables so that Kubernetes ConfigMaps
  can manage them without requiring a Key Vault round-trip.
* A single `Settings` dataclass is the canonical source of truth; every
  agent/module imports it via `get_settings()`.
* Data residency is enforced to **India Central** at the SDK level
  (Cosmos DB preferred locations, Azure Storage endpoint suffix, AKS node
  pool region).

Required Azure RBAC
-------------------
The AKS workload identity (MSI / federated credential) must hold:
  - Key Vault Secrets User  → on the Key Vault resource
  - Storage Blob Data Contributor → on the ADLS Gen2 account
  - Cosmos DB Built-in Data Contributor → on the Cosmos account

Dependencies
------------
  pip install azure-keyvault-secrets azure-identity pydantic pydantic-settings
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional imports — fail loudly at call-site
# ---------------------------------------------------------------------------
try:
    from azure.identity import (
        DefaultAzureCredential,
        ManagedIdentityCredential,
        WorkloadIdentityCredential,
    )
    from azure.keyvault.secrets import SecretClient
    _AZ_AVAILABLE = True
except ImportError:
    _AZ_AVAILABLE = False
    logger.warning(
        "azure-identity / azure-keyvault-secrets not installed. "
        "Falling back to environment-variable mode (dev only)."
    )


# ===========================================================================
# 1. CONSTANTS
# ===========================================================================

# ── Data residency ──────────────────────────────────────────────────────────
AZURE_REGION            = "centralindia"          # Azure region slug
AZURE_REGION_DISPLAY    = "India Central"          # Human-readable
COSMOS_PREFERRED_REGIONS = ["Central India"]       # Cosmos SDK preferred-locations list
STORAGE_ENDPOINT_SUFFIX = ".blob.core.windows.net" # standard; ADLS Gen2 uses .dfs.

# ── Key Vault secret names (the *keys* stored in KV, not the values) ────────
class KVSecret:
    # Gemini / LLM
    GEMINI_API_KEY          = "gemini-api-key"
    GEMINI_ENDPOINT         = "gemini-endpoint"
    GEMINI_MODEL_VERSION    = "gemini-model-version"

    # Azure OpenAI (fallback model)
    AOAI_API_KEY            = "aoai-api-key"
    AOAI_ENDPOINT           = "aoai-endpoint"
    AOAI_DEPLOYMENT         = "aoai-deployment-name"

    # Azure Cosmos DB
    COSMOS_CONNECTION_STRING = "cosmos-connection-string"
    COSMOS_DATABASE          = "cosmos-database-name"

    # Azure Data Lake Storage Gen2
    ADLS_CONNECTION_STRING  = "adls-connection-string"
    ADLS_ACCOUNT_NAME       = "adls-account-name"
    ADLS_CONTAINER_NAME     = "adls-container-name"

    # Azure Blob (MinIO-compatible staging)
    MINIO_ENDPOINT          = "minio-endpoint"
    MINIO_ACCESS_KEY        = "minio-access-key"
    MINIO_SECRET_KEY        = "minio-secret-key"
    MINIO_BUCKET            = "minio-bucket-name"

    # ServiceNow
    SERVICENOW_INSTANCE_URL = "servicenow-instance-url"
    SERVICENOW_CLIENT_ID    = "servicenow-client-id"
    SERVICENOW_CLIENT_SECRET= "servicenow-client-secret"
    SERVICENOW_USERNAME     = "servicenow-username"
    SERVICENOW_PASSWORD     = "servicenow-password"

    # Audit / HMAC
    AUDIT_HMAC_SECRET       = "audit-hmac-secret"
    PDF_SIGNING_KEY_PEM     = "pdf-signing-key-pem"

    # Alert / SMTP
    SMTP_HOST               = "smtp-host"
    SMTP_PORT               = "smtp-port"
    ALERT_SENDER            = "alert-sender-email"
    ALERT_RECIPIENTS        = "alert-recipients-csv"   # comma-separated list

    # Kill Switch
    KILL_SWITCH_WEBHOOK     = "kill-switch-webhook-url"


# ===========================================================================
# 2. SETTINGS DATACLASS
# ===========================================================================

@dataclass
class Settings:
    """
    Single source of truth for all runtime configuration.
    All string secrets are stored as plain `str`; callers must not log them.
    """

    # ── Region / residency ──────────────────────────────────────────────────
    azure_region:            str  = AZURE_REGION
    azure_region_display:    str  = AZURE_REGION_DISPLAY
    cosmos_preferred_regions: list[str] = field(default_factory=lambda: COSMOS_PREFERRED_REGIONS)

    # ── LLM / Gemini ────────────────────────────────────────────────────────
    gemini_api_key:          str  = ""
    gemini_endpoint:         str  = "https://generativelanguage.googleapis.com"
    gemini_model_version:    str  = "gemini-1.5-pro"

    # ── Azure OpenAI (fallback) ──────────────────────────────────────────────
    aoai_api_key:            str  = ""
    aoai_endpoint:           str  = ""
    aoai_deployment:         str  = "gpt-4o"

    # ── Cosmos DB ───────────────────────────────────────────────────────────
    cosmos_connection_string: str = ""
    cosmos_database:          str = "compliancedb"

    # ── ADLS Gen2 ────────────────────────────────────────────────────────────
    adls_connection_string:  str  = ""
    adls_account_name:       str  = ""
    adls_container_name:     str  = "compliance-docs"

    # ── MinIO / Blob staging ─────────────────────────────────────────────────
    minio_endpoint:          str  = ""
    minio_access_key:        str  = ""
    minio_secret_key:        str  = ""
    minio_bucket:            str  = "raw-documents"

    # ── ServiceNow ──────────────────────────────────────────────────────────
    servicenow_instance_url: str  = ""
    servicenow_client_id:    str  = ""
    servicenow_client_secret: str = ""
    servicenow_username:     str  = ""
    servicenow_password:     str  = ""

    # ── Audit logger ────────────────────────────────────────────────────────
    audit_hmac_secret:       bytes = field(default_factory=lambda: b"")
    pdf_signing_key_pem:     bytes = field(default_factory=lambda: b"")

    # ── Alerting ────────────────────────────────────────────────────────────
    smtp_host:               str        = ""
    smtp_port:               int        = 587
    alert_sender:            str        = ""
    alert_recipients:        list[str]  = field(default_factory=list)

    # ── Kill Switch ─────────────────────────────────────────────────────────
    kill_switch_webhook:     str        = ""
    kill_switch_error_threshold: float  = 0.02   # 2 %

    # ── Observability ───────────────────────────────────────────────────────
    log_level:               str        = "INFO"
    app_insights_conn_str:   str        = ""
    otel_exporter_endpoint:  str        = ""

    # ── AKS / runtime ───────────────────────────────────────────────────────
    aks_namespace:           str        = "compliance-pilot"
    max_concurrent_chains:   int        = 10
    pipeline_timeout_seconds: int       = 300


# ===========================================================================
# 3. KEY VAULT LOADER
# ===========================================================================

class KeyVaultLoader:
    """
    Pulls secrets from Azure Key Vault using DefaultAzureCredential.

    Credential resolution order (Azure SDK default):
      1. EnvironmentCredential (CI/CD)
      2. WorkloadIdentityCredential (AKS with federated identity)
      3. ManagedIdentityCredential (VM / App Service MSI)
      4. AzureCliCredential (developer workstation)

    Falls back to environment variables when Key Vault is unavailable
    (local development without VPN / KV access).
    """

    def __init__(self, vault_url: str):
        if not _AZ_AVAILABLE:
            raise ImportError(
                "azure-identity and azure-keyvault-secrets are required. "
                "pip install azure-identity azure-keyvault-secrets"
            )
        credential    = DefaultAzureCredential()
        self._client  = SecretClient(vault_url=vault_url, credential=credential)
        self._cache: dict[str, str] = {}
        logger.info("KeyVaultLoader initialised → %s", vault_url)

    # ------------------------------------------------------------------
    def get(self, secret_name: str, default: str = "") -> str:
        """Return a secret value, with in-process caching and env-var fallback."""
        if secret_name in self._cache:
            return self._cache[secret_name]

        try:
            value = self._client.get_secret(secret_name).value or default
            self._cache[secret_name] = value
            return value
        except Exception as exc:
            env_key = secret_name.upper().replace("-", "_")
            fallback = os.environ.get(env_key, default)
            if fallback:
                logger.debug(
                    "KV secret '%s' unavailable (%s); using env var %s",
                    secret_name, exc, env_key,
                )
                return fallback
            logger.warning("Secret '%s' not found in KV or env: %s", secret_name, exc)
            return default

    # ------------------------------------------------------------------
    def get_bytes(self, secret_name: str) -> bytes:
        raw = self.get(secret_name, "")
        return raw.encode() if raw else b""


# ===========================================================================
# 4. SETTINGS FACTORY
# ===========================================================================

def _build_settings_from_kv(vault_url: str) -> Settings:
    """Hydrate a Settings instance entirely from Key Vault."""
    kv = KeyVaultLoader(vault_url)

    recipients_raw = kv.get(KVSecret.ALERT_RECIPIENTS, "")
    recipients     = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    smtp_port_raw  = kv.get(KVSecret.SMTP_PORT, "587")
    try:
        smtp_port = int(smtp_port_raw)
    except ValueError:
        smtp_port = 587

    return Settings(
        # LLM
        gemini_api_key        = kv.get(KVSecret.GEMINI_API_KEY),
        gemini_endpoint       = kv.get(KVSecret.GEMINI_ENDPOINT,
                                       "https://generativelanguage.googleapis.com"),
        gemini_model_version  = kv.get(KVSecret.GEMINI_MODEL_VERSION, "gemini-1.5-pro"),

        # Azure OpenAI fallback
        aoai_api_key          = kv.get(KVSecret.AOAI_API_KEY),
        aoai_endpoint         = kv.get(KVSecret.AOAI_ENDPOINT),
        aoai_deployment       = kv.get(KVSecret.AOAI_DEPLOYMENT, "gpt-4o"),

        # Cosmos DB
        cosmos_connection_string = kv.get(KVSecret.COSMOS_CONNECTION_STRING),
        cosmos_database          = kv.get(KVSecret.COSMOS_DATABASE, "compliancedb"),

        # ADLS
        adls_connection_string = kv.get(KVSecret.ADLS_CONNECTION_STRING),
        adls_account_name      = kv.get(KVSecret.ADLS_ACCOUNT_NAME),
        adls_container_name    = kv.get(KVSecret.ADLS_CONTAINER_NAME, "compliance-docs"),

        # MinIO
        minio_endpoint    = kv.get(KVSecret.MINIO_ENDPOINT),
        minio_access_key  = kv.get(KVSecret.MINIO_ACCESS_KEY),
        minio_secret_key  = kv.get(KVSecret.MINIO_SECRET_KEY),
        minio_bucket      = kv.get(KVSecret.MINIO_BUCKET, "raw-documents"),

        # ServiceNow
        servicenow_instance_url  = kv.get(KVSecret.SERVICENOW_INSTANCE_URL),
        servicenow_client_id     = kv.get(KVSecret.SERVICENOW_CLIENT_ID),
        servicenow_client_secret = kv.get(KVSecret.SERVICENOW_CLIENT_SECRET),
        servicenow_username      = kv.get(KVSecret.SERVICENOW_USERNAME),
        servicenow_password      = kv.get(KVSecret.SERVICENOW_PASSWORD),

        # Audit
        audit_hmac_secret    = kv.get_bytes(KVSecret.AUDIT_HMAC_SECRET),
        pdf_signing_key_pem  = kv.get_bytes(KVSecret.PDF_SIGNING_KEY_PEM),

        # Alerting
        smtp_host         = kv.get(KVSecret.SMTP_HOST),
        smtp_port         = smtp_port,
        alert_sender      = kv.get(KVSecret.ALERT_SENDER),
        alert_recipients  = recipients,

        # Kill switch
        kill_switch_webhook = kv.get(KVSecret.KILL_SWITCH_WEBHOOK),
    )


def _build_settings_from_env() -> Settings:
    """
    Dev/CI fallback: construct Settings entirely from environment variables.
    No Key Vault interaction.
    """
    def _env(key: str, default: str = "") -> str:
        return os.environ.get(key, default)

    def _env_bytes(key: str) -> bytes:
        val = os.environ.get(key, "")
        return val.encode() if val else b""

    recipients_raw = _env("ALERT_RECIPIENTS_CSV", "")
    recipients     = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    return Settings(
        gemini_api_key           = _env("GEMINI_API_KEY"),
        gemini_endpoint          = _env("GEMINI_ENDPOINT",
                                       "https://generativelanguage.googleapis.com"),
        gemini_model_version     = _env("GEMINI_MODEL_VERSION", "gemini-1.5-pro"),
        aoai_api_key             = _env("AOAI_API_KEY"),
        aoai_endpoint            = _env("AOAI_ENDPOINT"),
        aoai_deployment          = _env("AOAI_DEPLOYMENT", "gpt-4o"),
        cosmos_connection_string = _env("COSMOS_CONNECTION_STRING"),
        cosmos_database          = _env("COSMOS_DATABASE", "compliancedb"),
        adls_connection_string   = _env("ADLS_CONNECTION_STRING"),
        adls_account_name        = _env("ADLS_ACCOUNT_NAME"),
        adls_container_name      = _env("ADLS_CONTAINER_NAME", "compliance-docs"),
        minio_endpoint           = _env("MINIO_ENDPOINT"),
        minio_access_key         = _env("MINIO_ACCESS_KEY"),
        minio_secret_key         = _env("MINIO_SECRET_KEY"),
        minio_bucket             = _env("MINIO_BUCKET", "raw-documents"),
        servicenow_instance_url  = _env("SERVICENOW_INSTANCE_URL"),
        servicenow_client_id     = _env("SERVICENOW_CLIENT_ID"),
        servicenow_client_secret = _env("SERVICENOW_CLIENT_SECRET"),
        servicenow_username      = _env("SERVICENOW_USERNAME"),
        servicenow_password      = _env("SERVICENOW_PASSWORD"),
        audit_hmac_secret        = _env_bytes("AUDIT_HMAC_SECRET"),
        pdf_signing_key_pem      = _env_bytes("PDF_SIGNING_KEY_PEM"),
        smtp_host                = _env("SMTP_HOST"),
        smtp_port                = int(_env("SMTP_PORT", "587")),
        alert_sender             = _env("ALERT_SENDER"),
        alert_recipients         = recipients,
        kill_switch_webhook      = _env("KILL_SWITCH_WEBHOOK_URL"),
        log_level                = _env("LOG_LEVEL", "INFO"),
        app_insights_conn_str    = _env("APPLICATIONINSIGHTS_CONNECTION_STRING"),
        otel_exporter_endpoint   = _env("OTEL_EXPORTER_OTLP_ENDPOINT"),
        aks_namespace            = _env("AKS_NAMESPACE", "compliance-pilot"),
        max_concurrent_chains    = int(_env("MAX_CONCURRENT_CHAINS", "10")),
        pipeline_timeout_seconds = int(_env("PIPELINE_TIMEOUT_SECONDS", "300")),
    )


# ===========================================================================
# 5. PUBLIC ENTRY POINT
# ===========================================================================

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the singleton Settings instance.

    Resolution order:
      1. If AZURE_KEYVAULT_URL env var is set → pull from Key Vault.
      2. Otherwise → construct from environment variables (dev mode).

    The result is cached for the lifetime of the process.
    Call `get_settings.cache_clear()` in tests to reset.
    """
    vault_url = os.environ.get("AZURE_KEYVAULT_URL", "")
    if vault_url and _AZ_AVAILABLE:
        logger.info("Loading settings from Azure Key Vault: %s  (region: %s)",
                    vault_url, AZURE_REGION_DISPLAY)
        try:
            settings = _build_settings_from_kv(vault_url)
            logger.info("Settings loaded from Key Vault ✓")
            return settings
        except Exception as exc:
            logger.error(
                "Key Vault load failed (%s) — falling back to env vars. "
                "This is NOT safe for production.", exc
            )

    logger.warning(
        "AZURE_KEYVAULT_URL not set or azure SDK unavailable. "
        "Loading settings from environment variables (dev mode)."
    )
    return _build_settings_from_env()