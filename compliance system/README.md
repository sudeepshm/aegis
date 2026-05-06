# Agentic Compliance System

An enterprise-grade, event-driven orchestration engine built with **LangGraph** designed to automate regulatory compliance. This system ingests regulatory documents (e.g., RBI, SEBI circulars), parses and extracts actionable compliance obligations using LLMs, routes them to the appropriate departments via a hybrid Rule/ML engine, and automatically creates Service Level Agreement (SLA) tracked tasks in **ServiceNow**.

The architecture prioritises **security, traceability, and resilience**, featuring a blockchain-style WORM (Write-Once-Read-Many) audit ledger, strict guardrails, automatic escalation thresholds, and graceful degradation under failure.

---

## 🏗️ Architecture Pipeline

The system runs completely headless, typically deployed on Azure Kubernetes Service (AKS), and executes a 6-stage LangGraph state machine:

1. **Ingestion Engine (`ingestion/`)**: Pulls documents from Azure Data Lake Storage (ADLS Gen2) or MinIO, enforcing SHA-256 deduplication and fallback OCR.
2. **Parser & Chunker (`parser/`)**: Splits raw text using heading-aware semantic chunking and sliding windows. Extracts entities via rule-based NER and embeds chunks via Azure OpenAI into Azure AI Search.
3. **MAP Generator (`llm/`)**: Generates a Master Audit Plan (MAP). Uses LLMs or Rule-Based fallbacks to extract obligations, map them to specific departments using SME rules, and validate outputs against confidence thresholds.
4. **Hybrid Router (`routing/`)**: Determines task priority and department routing using a combination of deterministic rules and ML text classification.
5. **Task Manager (`workflow/`)**: Synchronises generated MAP items with **ServiceNow** via OAuth2 Table API. Starts strict SLA countdown timers.
6. **Validation & Audit (`validation/`, `audit/`)**: Seals all outputs into an immutable, HMAC-SHA256 chained audit ledger stored in Cosmos DB, guaranteeing tamper-proof regulatory reporting.

---

## 🚀 Key Enterprise Features

* **WORM Audit Ledger**: Every action is cryptographically chained to the previous action, providing a mathematical guarantee of non-tampering.
* **Escalation Thresholds**: Any AI extraction or routing decision with a confidence score below `0.80` is automatically escalated to a Human-in-the-Loop (Compliance Officer).
* **Kill Switch**: Built-in rolling error rate monitoring. If failure rates exceed predefined thresholds, the system halts processing, alerts via Webhook, and flags itself as degraded.
* **Graceful Degradation**: If the LLM is down or external APIs (ServiceNow) fail, the pipeline degrades elegantly to Rule-Based engines and Dead-Letter Queues (DLQ) without crashing.
* **Security Hardened**: Mandates TLS 1.3 for all transport. Credentials and Secrets are strictly managed via Azure Key Vault.

---

## 💻 Local Development Setup

### Prerequisites
* Python 3.11+
* Docker & Docker Compose
* An Azure account (for KeyVault, CosmosDB, Document Intelligence, AI Search) - *Optional for local test runs*.

### Installation

1. **Clone the repository and enter the directory**:
   ```bash
   cd "compliance system"
   ```

2. **Set up a Python Virtual Environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   .\venv\Scripts\activate   # Windows
   ```

3. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```

4. **Environment Variables**:
   Copy the example environment file and fill in your details:
   ```bash
   cp .env.example .env
   ```
   *(Note: In production, rely on `AZURE_KEYVAULT_URL` instead of setting raw secrets in `.env`)*

---

## 🧪 Testing

The system includes a robust, zero-infrastructure unit testing suite that uses in-memory mock modes (no Azure credentials required to test core logic).

Run the tests with `pytest`:
```bash
pytest tests/ -v
```

Generate a coverage report:
```bash
pytest tests/ --cov=. --cov-report=term-missing
```

---

## 🐳 Deployment (Docker / Kubernetes)

### Docker Compose
Run the system locally using Docker Compose:
```bash
docker compose up --build
```
This builds a lightweight image using a non-root `compliance` user and enforces memory/cpu resource limits.

### Kubernetes (AKS)
1. Build and push the image to your Azure Container Registry (ACR).
2. Deploy the application to the `compliance-pilot` namespace.
3. Ensure the Pod has a Managed Identity (MSI) configured with read access to the Azure Key Vault.

---

## 📁 Repository Structure
```
.
├── audit/                  # Blockchain-style HMAC WORM ledger & reporting
├── ingestion/              # Blob fetching and duplicate registry 
├── llm/                    # 5-stage Master Audit Plan (MAP) generation
├── parser/                 # Semantic chunker & Azure Search indexer
├── routing/                # Rule-based and ML department router
├── tests/                  # 100+ automated unit tests
├── utils/                  # Cross-cutting helpers (Hashing, Masking, IST time)
├── validation/             # Pre/Post execution integrity guardrails
├── workflow/               # ServiceNow syncing and SLA management
├── config.py               # Centralised pydantic settings & Key Vault loading
├── main.py                 # Core LangGraph pipeline and entry point
├── Dockerfile              # Multi-stage production Docker build
├── docker-compose.yml      # Local dev container orchestration
├── requirements.txt        # Production dependencies
└── requirements-dev.txt    # Testing & linting dependencies
```
