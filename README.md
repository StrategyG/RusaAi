# RUSA-AI

Production-ready architecture, serving infrastructure, model blueprint, and execution engine for **RUSA-AI** — an independent, high-performance Large Language Model platform featuring Mixture-of-Experts (MoE), agent task envelope schema validation, and secure API serving.

---

## Key Features & Architecture

- **Independent Model Blueprint**: Decoder-only transformer architecture with 96 layers, 12,288 hidden size, 64 MoE experts (2 active per token), and RoPE positional embeddings.
- **Production API Serving**: FastAPI-powered inference backend supporting OpenAI-compatible `/v1/chat/completions`, task validation `/v1/validate-task`, and health telemetry (`/health`).
- **Agent-to-Agent (A2A) Schema**: Versioned JSON Schema validation (`schemas/a2a/task-envelope.schema.json`) for agent task payloads and autonomous workflows.
- **Enterprise Security & Reliability**: API Key authentication (`X-API-Key`), rate limiting, robust exception handling, non-root Docker runtime, and environment configuration.
- **Automated CI/CD**: Complete GitHub Actions workflow for linting, testing, container builds, and security scans.

---

## Repository Structure

```
RusaAi/
├── .github/
│   ├── workflows/
│   │   └── ci.yml               # Automated CI workflow (linting, tests, docker)
│   ├── ISSUE_TEMPLATE/           # GitHub issue templates for agent tasks
│   └── copilot-instructions.md   # AI collaboration & safety guidelines
├── docs/                         # Architecture & A2A integration documentation
├── llm-project/
│   ├── configs/                 # YAML configurations (model, training, inference)
│   │   ├── model.yaml
│   │   ├── training.yaml
│   │   └── inference.yaml
│   ├── model/
│   │   └── blueprint.py         # MoE model architecture specifications & blueprint
│   ├── serve/
│   │   └── app.py               # Production FastAPI serving engine
│   ├── tests/
│   │   ├── test_serve.py        # API unit & integration tests
│   │   └── test_blueprint.py    # Model blueprint unit tests
│   └── __init__.py
├── schemas/
│   └── a2a/
│       └── task-envelope.schema.json # Task payload validation schema
├── .env.example                  # Environment configuration template
├── Dockerfile                    # Production multi-stage Docker container
├── docker-compose.yml            # Container orchestration config
├── pyproject.toml                # Project metadata & build tool configuration
├── requirements.txt              # Production Python dependencies
└── README.md                     # Comprehensive project documentation
```

---

## Quick Start & Local Setup

### 1. Prerequisites
- Python 3.10 or higher
- Docker & Docker Compose (optional for containerized deployment)

### 2. Environment Setup
```bash
# Clone the repository
git clone StrategyG/RusaAi
cd RusaAi

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Variables
Copy `.env.example` to `.env` and set your secrets:
```bash
cp .env.example .env
```

---

## Running the Application

### Running Locally with Uvicorn
```bash
uvicorn llm-project.serve.app:app --host 0.0.0.0 --port 8000 --reload
```

### Running with Docker & Docker Compose
```bash
# Build and start container
docker-compose up --build -d

# Verify server status
curl localhost:8000/health
```

---

## Running Tests

Execute the automated test suite using pytest:
```bash
python -m pytest -v
```

---

## API Endpoints

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/health` | `GET` | Liveness check. Returns HTTP 200 with `"status": "healthy"` and service metadata. |
| `/v1/chat/completions` | `POST` | OpenAI-compatible chat completion inference endpoint |
| `/v1/validate-task` | `POST` | Validates agent task envelope payload against JSON Schema |

---

## Security & Governance

- **API Authorization**: Configure `RUSA_API_KEY` in environment to enforce header authentication.
- **Non-Root Execution**: Container runs under dedicated non-root user `appuser` (UID 10001).
- **Input Validation**: Strictly enforced Pydantic request models & JSON schema validation.

---

## License
This project is licensed under the MIT License.
