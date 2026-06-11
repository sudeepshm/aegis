# Aegis Compliance Engine

Aegis is an enterprise-grade, multi-agent AI pipeline designed to automate the ingestion, analysis, and execution of regulatory compliance mandates. By leveraging a state-machine architecture and real-time topological risk assessment, Aegis transforms static regulatory text into actionable, dependency-mapped operational workflows.

##  Core Architecture & Features

* **Multi-Agent State Machine (LangGraph):** Utilizes a 6-stage autonomous agent swarm to parse complex regulatory PDFs, extract absolute obligations, and decompose them into departmental tasks.
* **Topological Risk Assessment (DAG & BFS):** Implements a custom Breadth-First Search (BFS) algorithm to traverse a Directed Acyclic Graph (DAG) of compliance tasks. Calculates the cascade risk and "blast radius" of downstream operational delays.
* **Real-Time Data Streaming:** Features an asynchronous, event-driven architecture connecting a Next.js frontend to a Python FastAPI backend via live WebSockets.
* **Intelligent Guardrails & Conflict Detection:** Automatically cross-references incoming mandates against simulated internal corporate policies to flag contradictions and compliance tensions.
* **Automated Escalation Routing:** Dynamically routes alert notifications to assigned departments (e.g., IT Operations, Legal, Risk Management) based on mathematical delay thresholds.
* **Dynamic Node Visualization:** Parses sanitized JSON payloads to render interactive, real-time dependency graphs using React Flow.

## 🛠️ Technology Stack

**Frontend**
* Framework: Next.js (React)
* Visualization: React Flow
* Styling: CSS / Tailwind 

**Backend & AI Engine**
* Framework: Python (FastAPI)
* Communication: WebSockets
* Orchestration: LangGraph
* LLM Integration: Google Gemini 2.5 Pro (via API)

## ⚙️ System Workflow

1. **Ingestion Phase:** The system receives raw regulatory text/documents via the frontend UI.
2. **Agentic Processing:** The LangGraph swarm processes the input, extracting non-negotiable mandates and generating a structured JSON state dictionary.
3. **Graph Construction:** The backend constructs a mathematical DAG representing task dependencies.
4. **Validation:** The Guardrail Agent evaluates the generated nodes against the internal policy knowledge base.
5. **Execution & Routing:** Approved workflows are streamed back to the client via WebSockets, rendering the visual DAG and triggering the automated SMTP escalation router.

## 💻 Local Setup & Installation

### Prerequisites
* Node.js (v18+)
* Python 3.10+
* Google Gemini API Key

### Backend Setup
```bash
# Navigate to the backend directory
cd backend

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
# Create a .env file with your GEMINI_API_KEY and SMTP credentials

# Run the FastAPI server
uvicorn main:app --reload --port 8000

#** frontend support**
# Navigate to the frontend directory
cd frontend

# Install dependencies
npm install

# Run the development server
npm run dev
