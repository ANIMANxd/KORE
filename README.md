# KORE - On-Premise Context Engine

An AI-powered system that ingests fragmented company data from sources like Slack, Notion, GitHub, and Jira, extracts hidden business rules using LLM reasoning, and outputs structured, validated YAML/JSON skill files that AI agents can enforce reliably.

**Phase**: Phase 1 (MVP) — Complete. Provider abstraction + Slack ingestion + pgvector storage + LangGraph extraction pipeline + CLI + tests.

---

## What's Built So Far

### 1. Provider Abstraction Layer (`providers/`)

The entire system is provider-agnostic. Users bring their own LLM and embedding models — cloud or local.

- **`registry.py`** — Master registry of 12 supported providers (OpenAI, Anthropic, Gemini, DeepSeek, Mistral, Cohere, Azure, Bedrock, Ollama, LM Studio, LocalAI, Custom). Defines what each provider needs (API key, base URL, LiteLLM prefix). **No model names are hardcoded anywhere.**
- **`config.py`** — Loads and validates `provider.config.yaml`. Validates provider names against registry, enforces API keys for cloud providers, auto-fills default base URLs for local providers. Singleton pattern for cached config.
- **`llm.py`** — **The only file that calls LiteLLM.** Wraps all LLM calls with:
  - JSON repair loop (mandatory — retries up to 3 times with repair instructions when local models produce malformed JSON)
  - Rate-limit retry with exponential backoff
  - `response_format={"type": "json_object"}` for supported providers
  - `JSONRepairFailed` exception with raw output attached
- **`embedder.py`** — **The only file that handles embeddings.**
  - Single and batch embedding via LiteLLM
  - Direct HTTP to Ollama's `/api/embeddings` endpoint for reliability
  - Batch size 100, retry logic, progress tracking
  - Dimension validation against config

### 2. CLI Setup (`run.py`)

Interactive configuration wizard using Click and Rich:

```bash
python run.py setup   # Guided provider config setup
python run.py status  # Show current configuration
```

- Deployment mode selection (cloud / local / hybrid)
- Provider selection via Rich tables
- Never suggests model names — points users to provider documentation
- Tests LLM and embedding connections live before saving
- Auto-adds `provider.config.yaml` to `.gitignore`

### 3. Data Ingestion (`ingestion/`)

- **`loader.py`** — Parses local Slack export JSON directories into LlamaIndex `Document` objects. Filters bot/system/empty messages. Preserves metadata: `source_type`, `channel`, `timestamp`, `user_id`, `thread_ts`, `message_id`.
- **`chunker.py`** — Source-aware chunking strategies:
  - **Slack**: 15-minute time-window grouping (conversation bursts)
  - **GitHub**: Markdown + diff-aware, splits at H2/H3 headings, never splits code blocks
  - **Notion**: Heading-aware, tables preserved as single chunks
  - **Jira**: Field-based — description + individual comment chunks
  - **Generic**: `SentenceSplitter` fallback (512 tokens, 50 overlap)

### 4. Vector Storage (`storage/`)

- **`vector_store.py`** — PostgreSQL + pgvector:
  - `init_db()` — Creates `document_chunks` table with validated vector dimensions
  - `store_chunks()` — Embeds and stores chunks, skips duplicates by chunk_id
  - `similarity_search()` — Cosine similarity search with configurable threshold
  - `get_chunks_by_ids()` — Fetch chunks by ID
  - Connection retry logic (3 attempts with exponential backoff)

### 5. Extraction Engine (`engine/`)

LangGraph pipeline with conditional edges and loop guard:

- **`state.py`** — `ExtractionState` TypedDict with strict typing: query, retrieved_chunks, candidate_rule, confidence, category, source_refs, verification_status, extraction_meta
- **`graph.py`** — Pipeline wiring:
  - `START → loop_guard → retriever → extractor → verifier → formatter → END`
  - Conditional edges: insufficient context → error, low confidence → rejected, contradictions → needs_review
  - Loop guard: forces END if `iteration_count > 5`
  - Public API: `run_extraction(query)`
- **`nodes/retriever.py`** — Pure vector similarity search (no LLM). Deduplicates by content. Minimum 3 chunks required.
- **`nodes/extractor.py`** — LLM rule extraction via `complete_json()`. System prompt enforces JSON-only output. Confidence thresholds: <0.3 rejected, 0.3–0.6 needs_review.
- **`nodes/verifier.py`** — LLM cross-check against source chunks. Detects contradictions. Can adjust rule text and confidence.
- **`nodes/formatter.py`** — Pure formatting (no LLM). Builds validated `BusinessRule` Pydantic model with enriched source references.

### 6. Output Layer (`output/`)

- **`schema.py`** — Pydantic v2 models:
  - `SourceRef` — chunk_id, source_type, channel, timestamp, excerpt (auto-truncated to 100 chars)
  - `ExtractionMeta` — provider, model, deployment_mode, json_repair_attempts
  - `BusinessRule` — UUID auto-generated, UTC timestamps, literal validation, cross-field validators
  - `RulesFile` — YAML serialization with computed `total_rules`
- **`writer.py`** — File persistence:
  - `save_rule()` — writes `{timestamp}_{category}_{id}.yaml`
  - `load_rules()` — validates all YAML files against schema, skips invalid
  - `update_rule()` — finds by UUID, applies updates, increments version

### 7. Full CLI (`run.py`)

All commands built with Click + Rich:

```bash
python run.py setup          # Interactive provider configuration
python run.py ingest --source slack --path data/sample   # Load → chunk → embed → store
python run.py extract --query "refund policy" --save     # Full extraction pipeline
python run.py review         # Interactive human review (approve/reject/edit)
python run.py status         # Rich dashboard (providers, chunks, rules, repair stats)
python run.py reset-db       # Drop and recreate document_chunks table
python run.py --verbose ...  # Enable debug logging on any command
```

### 8. Test Suite (`tests/`)

32 tests, all passing. No real LLM calls in tests — everything mocked.

- **`test_json_repair.py`** (5) — Repair loop, markdown fences, exhaustion, counter
- **`test_chunker.py`** (7) — Slack windowing, GitHub code blocks, generic fallback
- **`test_extractor.py`** (5) — Success, parse_failed, confidence thresholds, meta
- **`test_verifier.py`** (6) — Verified, rejected, contradictions, adjusted text
- **`test_schema.py`** (9) — Pydantic validations, excerpt truncation, YAML round-trip

Run: `pytest tests/ -v`

### 9. Test Data (`data/sample/`)

Fake Slack export for "Acme Corp" (50-person startup):
- 3 channels: `#engineering`, `#ops`, `#customer-success`
- 157 messages across 50 fictional users
- 3-month date span
- 8 hidden business rules (refund policy, deployment schedule, pricing exceptions, escalation, SLA, on-call rotation, data retention, churn response)
- 1 intentional contradiction (on-call swap rules differ between `#engineering` and `#ops`)
- `ground_truth_rules.txt` for verification

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **LLM Abstraction** | LiteLLM | Single interface for all providers (OpenAI, Anthropic, Ollama, LM Studio, etc.) |
| **Embedding** | LiteLLM + direct HTTP (Ollama) | Text-to-vector for local and cloud models |
| **Vector Store** | PostgreSQL + pgvector | Local, production-grade similarity search |
| **Data Connectors** | LlamaIndex (`llama-index-readers-slack`) | Official Slack export parser |
| **Chunking** | Custom + LlamaIndex `SentenceSplitter` | Source-aware strategies per document type |
| **Orchestration** | LangGraph | Stateful reasoning pipeline with repair loops |
| **Schema Validation** | Pydantic v2 | Output rule validation |
| **CLI** | Click + Rich | Interactive setup and status commands |
| **Config** | PyYAML + python-dotenv | Provider config and environment variables |
| **JSON Repair** | `json` + `json5` + `demjson3` | Lenient parsing for local model failures |

---

## Project Structure

```
KORE/
├── AGENTS.md                    # Agent context and conventions
├── .env                         # Environment variables (gitignored)
├── .env.example                 # Example environment config
├── provider.config.yaml         # User's LLM/embedding provider selection (gitignored)
├── requirements.txt             # Python dependencies
├── run.py                       # CLI entry point
│
├── providers/                   # Provider abstraction (the ONLY files touching LLM/embeddings)
│   ├── __init__.py
│   ├── registry.py              # Supported providers and their requirements
│   ├── config.py                # Config loader and validator
│   ├── llm.py                   # LiteLLM wrapper + JSON repair loop
│   └── embedder.py              # Embedding wrapper
│
├── ingestion/                   # Data loading and chunking
│   ├── __init__.py
│   ├── loader.py                # Slack export parser
│   └── chunker.py               # Source-aware chunking strategies
│
├── storage/                     # Vector database
│   ├── __init__.py
│   └── vector_store.py          # pgvector read/write
│
├── engine/                      # LangGraph extraction pipeline
│   ├── __init__.py
│   ├── state.py                 # TypedDict state definition
│   ├── graph.py                 # Graph wiring and runner
│   └── nodes/
│       ├── retriever.py         # Vector similarity search
│       ├── extractor.py         # LLM rule extraction
│       ├── verifier.py          # LLM cross-check
│       └── formatter.py         # Pydantic formatting
│
├── output/                      # Schema validation and persistence
│   ├── __init__.py
│   ├── schema.py                # Pydantic v2 models
│   └── writer.py                # YAML file writer/loader/updater
│
├── tests/                       # Test suite (32 tests, all passing)
│   ├── __init__.py
│   ├── test_json_repair.py
│   ├── test_chunker.py
│   ├── test_extractor.py
│   ├── test_verifier.py
│   └── test_schema.py
│
├── data/
│   └── sample/                  # Fake test data (Acme Corp Slack export)
│
└── rules/
    └── extracted/               # Output YAML files (created at runtime)
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure providers

```bash
python run.py setup
```

This interactively guides you through selecting:
- Deployment mode (cloud / local / hybrid)
- LLM provider and model name
- Embedding provider and model name
- API keys (cloud) or base URLs (local)

### 3. Set environment variables

```bash
cp .env.example .env
# Edit .env with your database URL:
# DATABASE_URL=postgresql://user:password@localhost:5432/kore
```

### 4. Initialize database

```python
from storage.vector_store import init_db
init_db()
```

### 5. Ingest sample data

```bash
python run.py ingest --source slack --path data/sample
```

### 6. Extract a rule

```bash
python run.py extract --query "refund policy" --save
```

Output: Rich panel with rule text, confidence, status, source references.

### 7. Review flagged rules

```bash
python run.py review
```

Interactive approval: `(a)pprove`, `(r)eject`, `(e)dit`, `(s)kip`.

### 8. Check system status

```bash
python run.py status
```

Rich dashboard: providers, chunks by source, rules by status/category, repair stats.

### 9. Run tests

```bash
pytest tests/ -v
```

---

## Supported LLM Providers

| Provider | Type | API Key | Base URL |
|----------|------|---------|----------|
| OpenAI | Cloud | Yes | No |
| Anthropic | Cloud | Yes | No |
| Gemini | Cloud | Yes | No |
| DeepSeek | Cloud | Yes | No |
| Mistral | Cloud | Yes | No |
| Cohere | Cloud | Yes | No |
| Azure OpenAI | Cloud | Yes | Yes |
| AWS Bedrock | Cloud | Yes | No |
| Ollama | Local | No | Yes (default: `http://localhost:11434`) |
| LM Studio | Local | No | Yes (default: `http://localhost:1234/v1`) |
| LocalAI | Local | No | Yes |
| Custom | Local | No | Yes |

---

## Known Issues & Quirks

- **Local embedding similarity scores are lower** (~0.50-0.60) compared to OpenAI (~0.80+). Set `SIMILARITY_THRESHOLD=0.40` for local deployments.
- **LiteLLM requires a dummy API key** for local providers (Ollama, LM Studio). Both `llm.py` and `embedder.py` handle this automatically.
- **Ollama embeddings use direct HTTP** instead of LiteLLM for reliability (`/api/embeddings` endpoint).

---

## What's Next (Phase 2)

- **Multi-source ingestion** — Notion, GitHub, Jira via LlamaHub readers
- **Full chunking strategies** — GitHub diff-aware, Notion heading-aware, Jira field-based
- **Conflict detection** — Cross-source contradiction detection in verifier
- **REST API** — FastAPI layer for agent integration
- **Docker Compose** — Full local deployment with PostgreSQL

---

## License

All rights reserved @KORE-Labs
