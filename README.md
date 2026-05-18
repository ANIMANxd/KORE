# KORE - On-Premise Context Engine

An AI-powered system that ingests fragmented company data from sources like Slack, Notion, GitHub, and Jira, extracts hidden business rules using LLM reasoning, and outputs structured, validated YAML/JSON skill files that AI agents can enforce reliably.

**Phase**: Phase 4 (Rule Lifecycle) — Contradiction detection complete. Vector store backend abstraction (LanceDB + pgvector) + live connectors for Slack, GitHub, Notion, and Jira all built.

---

## What's Built So Far

### 1. Provider Abstraction Layer (`providers/`)

The entire system is provider-agnostic. Users bring their own LLM and embedding models — cloud or local. **No model names are hardcoded anywhere.**

- **`registry.py`** - Master registry of 12 supported providers (OpenAI, Anthropic, Gemini, DeepSeek, Mistral, Cohere, Azure, Bedrock, Ollama, LM Studio, LocalAI, Custom). Defines what each provider needs (API key, base URL, LiteLLM prefix).
- **`config.py`** - Loads and validates `provider.config.yaml`. Validates provider names against registry, enforces API keys for cloud providers, auto-fills default base URLs for local providers. Reads vector store backend selection. Singleton pattern for cached config.
- **`llm.py`** - **The only file that calls LiteLLM.** Wraps all LLM calls with:
  - JSON repair loop (mandatory — retries up to 3 times with repair instructions when local models produce malformed JSON)
  - Rate-limit retry with exponential backoff
  - `response_format={"type": "json_object"}` for supported providers
  - `JSONRepairFailed` exception with raw output attached
- **`embedder.py`** - **The only file that handles embeddings.**
  - Single and batch embedding via LiteLLM
  - Direct HTTP to Ollama's `/api/embeddings` endpoint for reliability
  - Batch size 100, retry logic, progress tracking
  - Dimension validation against config

### 2. Vector Store Backend Abstraction (`storage/`)

Hybrid backend architecture — identical public interface regardless of backend:

- **`vector_store.py`** - Public interface (backend-agnostic singleton). All modules outside `storage/` call these functions only.
- **`backends/base.py`** - Abstract base class `VectorStoreBackend`
- **`backends/lancedb_backend.py`** - **Default.** Zero-config, file-based, embedded. Stores data at `~/.kore/data/vectors/`. Uses PyArrow schema. Handles deduplication and cosine similarity search.
- **`backends/pgvector_backend.py`** - Enterprise option for existing PostgreSQL servers. Full connection retry logic, dimension validation, cosine similarity search.

**Backend selection** is read from `provider.config.yaml`. Switching backends is one config line.

### 3. CLI Setup (`run.py`)

Interactive configuration wizard using Click and Rich:

```bash
python run.py setup          # Interactive provider + vector store configuration
python run.py slack-setup    # Interactive Slack bot setup (token, channels, test connection)
python run.py github-setup   # Interactive GitHub setup (token, repo, branch, test connection)
python run.py notion-setup   # Interactive Notion setup (integration token, test connection)
python run.py jira-setup     # Interactive Jira setup (server, email, API token, project keys, test connection)
python run.py status         # Show system status dashboard
```

### 4. Data Ingestion (`ingestion/`)

- **`loader.py`** - Multi-source loader supporting local exports and live APIs:
  - **Slack (local export)**: Parses Slack export JSON directories into LlamaIndex `Document` objects
  - **Slack (live API)**: Connects via Bot Token, fetches messages in real-time with pagination
  - **GitHub (live API)**: Reads repository files (filtered by extension) + issues/PRs. Excludes `node_modules`, `.git`, `dist`, `build`, etc. by default.
  - **Notion (live API)**: Reads all accessible pages and databases via integration token
  - **Jira (live API)**: Reads all issues from specified project keys via JQL. Auto-paginates through large result sets.
  - Filters bot/system/empty messages per source
  - Preserves source-aware metadata: `source_type`, channel/timestamp (Slack), repo/branch/file_path (GitHub), page_id (Notion), ticket_id/status/assignee (Jira)
- **`chunker.py`** - Source-aware chunking strategies:
  - **Slack**: 15-minute time-window grouping (conversation bursts)
  - **GitHub**: Markdown + diff-aware, splits at H2/H3 headings, never splits code blocks
  - **Notion**: Heading-aware, tables preserved as single chunks
  - **Jira**: Field-based — description + individual comment chunks
  - **Generic**: `SentenceSplitter` fallback (512 tokens, 50 overlap)

### 5. Extraction Engine (`engine/`)

LangGraph pipeline with conditional edges and loop guard:

- **`state.py`** - `ExtractionState` TypedDict with strict typing
- **`graph.py`** - Pipeline wiring:
  - `START -> loop_guard -> retriever -> extractor -> verifier -> formatter -> END`
  - Conditional edges: insufficient context -> error, low confidence -> rejected, contradictions -> needs_review
  - Loop guard: forces END if `iteration_count > 5`
  - Public API: `run_extraction(query)`
- **`nodes/retriever.py`** - Pure vector similarity search (no LLM). Deduplicates by content. Minimum 3 chunks required.
- **`nodes/extractor.py`** - LLM rule extraction via `complete_json()`. System prompt enforces JSON-only output. Confidence thresholds: <0.3 rejected, 0.3-0.6 needs_review.
- **`nodes/verifier.py`** - LLM cross-check against source chunks. Detects contradictions. Can adjust rule text and confidence.
- **`nodes/formatter.py`** - Pure formatting (no LLM). Builds validated `BusinessRule` Pydantic model with enriched source references.
- **`contradiction_detector.py`** - Post-save rule lifecycle:
  - `find_contradictions()` - Compares a new rule against all existing rules in the same category via LLM. Only same-category rules are checked.
  - `ContradictionReport` dataclass - rule_id_a/b, rule_text_a/b, description, severity (high/medium/low), detected_at
  - `save_contradiction_report()` / `load_contradiction_reports()` - YAML persistence to `rules/contradictions/`
  - Auto-triggered after every `extract --save`. Prints red warning panel when conflicts found.
  - Gracefully handles `JSONRepairFailed` during contradiction check (assumes no contradiction, logs warning)
- **`expiry_detector.py`** - Rule staleness detection:
  - `check_rule_expiry()` - Parses all source-ref timestamps, finds the most recent, and compares against a days threshold (default 90). Returns `ExpiryReport` or `None`.
  - `scan_all_rules()` - Loads all rules and returns expired ones sorted by age descending.
  - Supports multiple timestamp formats: ISO-8601, date-only, with/without timezone.
- **`reextractor.py`** - Rule re-extraction engine:
  - `find_affected_rules(new_chunk_ids, existing_rules)` - Computes overlap ratio between new chunk IDs and each rule's source_refs. Returns rule IDs where overlap ≥ threshold (default 0.85).
  - `reextract_rule(rule, query=None)` - Re-runs the full LangGraph pipeline for an existing rule. Uses rule text as default query, or accepts a custom query.
  - CLI: `python run.py reextract --rule-id <uuid>` shows old vs new side-by-side, prompts for replacement, auto-increments version.
  - CLI: `python run.py check-expiry --days 90` prints a Rich table with rule excerpt, category, last referenced date, and days old.

### 6. Output Layer (`output/`)

- **`schema.py`** - Pydantic v2 models:
  - `SourceRef` - chunk_id, source_type, channel, timestamp, excerpt (auto-truncated to 100 chars)
  - `ExtractionMeta` - provider, model, deployment_mode, json_repair_attempts
  - `BusinessRule` - UUID auto-generated, UTC timestamps, literal validation, cross-field validators
  - `RulesFile` - YAML serialization with computed `total_rules`
- **`writer.py`** - File persistence:
  - `save_rule()` - writes `{timestamp}_{category}_{id}.yaml`
  - `load_rules()` - validates all YAML files against schema, skips invalid
  - `update_rule()` - finds by UUID, applies updates, increments version
- **`skills_writer.py`** - Agent-facing export:
  - `export_skills_json()` - Exports verified, human-approved rules to `skills.json`
  - Only includes rules where `verification_status == "verified"` AND `approved_by is not None`
  - Output format: `{version, generated_at, source, total_skills, skills[]}` with id, name, description, category, confidence, constraints, sources, approved, version

### 7. Full CLI (`run.py`)

All commands built with Click + Rich:

```bash
# Setup
python run.py setup          # Interactive provider + vector store configuration
python run.py slack-setup    # Interactive Slack bot setup
python run.py github-setup   # Interactive GitHub setup
python run.py notion-setup   # Interactive Notion integration setup
python run.py jira-setup     # Interactive Jira Cloud setup

# Ingestion
python run.py ingest --source slack --path data/sample     # Local Slack export
python run.py ingest --source slack --live                 # Live Slack API
python run.py ingest --source github --live                # Live GitHub API
python run.py ingest --source notion --live                # Live Notion API
python run.py ingest --source jira --live                  # Live Jira API

# Extraction & review
python run.py extract --query "refund policy" --save       # Full extraction pipeline
python run.py review         # Interactive human review (approve/reject/edit)
python run.py check-expiry   # Scan rules for staleness (default: >90 days)
python run.py check-expiry --days 60 --dir rules/extracted/  # Custom threshold
python run.py reextract --rule-id <uuid>                   # Re-extract an existing rule
python run.py reextract --rule-id <uuid> --query "new phrasing"  # Custom query
python run.py export-skills                               # Export approved rules to skills.json
python run.py export-skills --output skills.json --dir rules/extracted/  # Custom paths

# System
python run.py status         # Rich dashboard (providers, chunks, rules, repair stats)
python run.py reset-db       # Drop and recreate vector store
python run.py --verbose ...  # Enable debug logging on any command
```

### 8. Test Suite (`tests/`)

92 tests, all passing. No real LLM calls or network requests in tests — everything mocked.

- **`test_json_repair.py`** (5) - Repair loop, markdown fences, exhaustion, counter
- **`test_chunker.py`** (7) - Slack windowing, GitHub code blocks, generic fallback
- **`test_extractor.py`** (5) - Success, parse_failed, confidence thresholds, meta
- **`test_verifier.py`** (6) - Verified, rejected, contradictions, adjusted text
- **`test_schema.py`** (9) - Pydantic validations, excerpt truncation, YAML round-trip
- **`test_connectors.py`** (11) - All data sources and vector store backends mocked:
  - Slack, GitHub, Notion, Jira loader metadata and routing
  - Source-aware chunker selection (GitHub, Jira)
  - LanceDB backend store/search with mocked PyArrow and lancedb
  - Pgvector backend interface with mocked psycopg2 and pgvector
  - Backend switching via config (LanceDB ↔ pgvector, invalid backend)
- **`test_contradiction_detector.py`** (13) - Rule lifecycle contradiction detection:
  - ContradictionReport dataclass creation and YAML round-trip
  - No contradiction, contradiction found, same-category filtering, self-skip
  - Graceful handling of JSONRepairFailed during check
  - Multiple existing rules checked, invalid severity defaults to medium
  - Save/load persistence and malformed file skipping
- **`test_expiry_detector.py`** (15) - Rule staleness detection:
  - Timestamp parsing (ISO-8601, offset, date-only, unparseable)
  - Fresh rule returns None, stale rule returns ExpiryReport
  - Most-recent timestamp used, no source refs handled gracefully
  - scan_all_rules filtering, sorting by age descending, empty directory
- **`test_reextractor.py`** (10) - Rule re-extraction:
  - find_affected_rules: exact/partial overlap, threshold sensitivity, multiple rules
  - Empty new chunks, rules with no source refs skipped
  - reextract_rule: default query from rule text, custom query override
- **`test_skills_writer.py`** (11) - Skills export:
  - Source string formatting (channel + timestamp, channel-only, timestamp-only, fallback)
  - Rule-to-skill conversion (id, name, description, category, confidence, constraints, sources, approved, version)
  - Export filtering: only verified + approved_by rules included
  - Empty export when no approved rules, ISO timestamp generated_at, nested directory creation

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
| **Vector Store** | **LanceDB** (default) + **pgvector** (enterprise) | Hybrid backend — LanceDB is zero-config file-based; pgvector connects to existing PostgreSQL |
| **Data Connectors** | LlamaHub readers (`llama-index-readers-slack`, `-github`, `-notion`, `-jira`) | Official readers for Slack, GitHub, Notion, Jira |
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
├── provider.config.yaml         # User's LLM/embedding/vector-store config (gitignored)
├── requirements.txt             # Python dependencies
├── run.py                       # CLI entry point
│
├── providers/                   # Provider abstraction (the ONLY files touching LLM/embeddings)
│   ├── __init__.py
│   ├── registry.py              # Supported providers and their requirements
│   ├── config.py                # Config loader and validator (includes vector store config)
│   ├── llm.py                   # LiteLLM wrapper + JSON repair loop
│   └── embedder.py              # Embedding wrapper
│
├── ingestion/                   # Data loading and chunking
│   ├── __init__.py
│   ├── loader.py                # Slack (local + live), GitHub (live), Notion (live), Jira (live)
│   └── chunker.py               # Source-aware chunking strategies
│
├── storage/                     # Vector database (backend-agnostic)
│   ├── __init__.py
│   ├── vector_store.py          # Public interface — identical for all backends
│   └── backends/
│       ├── base.py              # Abstract base class
│       ├── lancedb_backend.py   # LanceDB (default, zero-config)
│       └── pgvector_backend.py  # pgvector (enterprise option)
│
├── engine/                      # LangGraph extraction pipeline
│   ├── __init__.py
│   ├── state.py                 # TypedDict state definition
│   ├── graph.py                 # Graph wiring and runner
│   ├── contradiction_detector.py# Rule lifecycle: detect conflicting rules
│   ├── expiry_detector.py       # Rule lifecycle: detect stale rules
│   ├── reextractor.py           # Rule lifecycle: re-extract affected rules
│   └── nodes/
│       ├── retriever.py         # Vector similarity search
│       ├── extractor.py         # LLM rule extraction
│       ├── verifier.py          # LLM cross-check
│       └── formatter.py         # Pydantic formatting
│
├── output/                      # Schema validation and persistence
│   ├── __init__.py
│   ├── schema.py                # Pydantic v2 models
│   ├── writer.py                # YAML file writer/loader/updater
│   └── skills_writer.py         # skills.json export for agents
│
├── tests/                       # Test suite (92 tests, all passing)
│   ├── __init__.py
│   ├── test_json_repair.py
│   ├── test_chunker.py
│   ├── test_extractor.py
│   ├── test_verifier.py
│   ├── test_schema.py
│   ├── test_connectors.py
│   ├── test_contradiction_detector.py
│   ├── test_expiry_detector.py
│   ├── test_reextractor.py
│   └── test_skills_writer.py
│
├── data/
│   └── sample/                  # Fake test data (Acme Corp Slack export)
│
└── rules/
    ├── extracted/               # Output YAML files (created at runtime)
    └── contradictions/          # Contradiction reports (created at runtime)
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure LLM providers and vector store

```bash
python run.py setup
```

This interactively guides you through selecting:
- Deployment mode (cloud / local / hybrid)
- LLM provider and model name
- Embedding provider and model name
- API keys (cloud) or base URLs (local)
- **Vector store backend** (LanceDB recommended, or PostgreSQL + pgvector)
- Data directory (LanceDB) or PostgreSQL connection URL (pgvector)

### 3. Set environment variables

```bash
cp .env.example .env
# Edit .env with your source credentials as needed:
#
# Slack (live API)
# SLACK_BOT_TOKEN=xoxb-your-token
# SLACK_CHANNEL_IDS=C01234567,C09876543
# SLACK_EARLIEST_DATE=2024-01-01
#
# GitHub (live API)
# GITHUB_TOKEN=ghp_your-token
# GITHUB_REPO=owner/repo
# GITHUB_BRANCH=main
# GITHUB_EXTENSIONS=.py .md .yaml .yml .ts .js
#
# Notion (live API)
# NOTION_TOKEN=ntn_your-token
#
# Jira (live API)
# JIRA_URL=https://company.atlassian.net
# JIRA_USERNAME=your-email@example.com
# JIRA_API_TOKEN=your-token
# JIRA_PROJECT_KEYS=KAN,PROJ
```

### 4. Initialize vector store

The vector store is initialized automatically on first ingestion. No manual step required.

Or reset it anytime:

```bash
python run.py reset-db
```

### 5. Ingest data

#### Slack

**Local export:**

```bash
python run.py ingest --source slack --path data/sample
```

**Live API:**

```bash
python run.py slack-setup
python run.py ingest --source slack --live
```

#### GitHub

```bash
python run.py github-setup
python run.py ingest --source github --live
```

#### Notion

```bash
python run.py notion-setup
python run.py ingest --source notion --live
```

#### Jira

```bash
python run.py jira-setup
python run.py ingest --source jira --live
```

Each setup wizard will:
1. Guide you through creating the necessary credentials
2. Prompt for each value with context-specific hints
3. Test the connection live
4. Save everything to `.env`
5. Optionally run ingestion immediately

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

Rich dashboard: providers, vector store backend, chunks by source, rules by status/category, repair stats.

### 9. Run tests

```bash
pytest tests/ -v
```

---

## Supported Data Sources

| Source | Mode | Setup Command | Requirements |
|--------|------|---------------|--------------|
| **Slack** | Local export + Live API | `python run.py slack-setup` | Slack app with Bot Token Scopes: `channels:history`, `channels:read`, `users:read` |
| **GitHub** | Live API only | `python run.py github-setup` | Personal Access Token with `repo` or `public_repo` scope |
| **Notion** | Live API only | `python run.py notion-setup` | Internal Integration Token + share each page/database with the integration |
| **Jira** | Live API only | `python run.py jira-setup` | API Token from Atlassian account settings |

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

## Vector Store Backends

| Backend | Default | When to use | Requirements |
|---------|---------|-------------|--------------|
| **LanceDB** | Yes | Zero-config, file-based, ships in binary | Nothing — embedded |
| **pgvector** | No | Enterprise with existing PostgreSQL | PostgreSQL + pgvector extension |

Switching backends is one line in `provider.config.yaml`:

```yaml
vector_store:
  backend: lancedb   # or pgvector
  lancedb:
    data_dir: ~/.kore/data
  pgvector:
    url: postgresql://user:pass@host:5432/dbname
```

---

## Known Issues & Quirks

- **Local embedding similarity scores are lower** (~0.50-0.60) compared to OpenAI (~0.80+). Set `SIMILARITY_THRESHOLD=0.40` for local deployments.
- **LiteLLM requires a dummy API key** for local providers (Ollama, LM Studio). Both `llm.py` and `embedder.py` handle this automatically.
- **Ollama embeddings use direct HTTP** instead of LiteLLM for reliability (`/api/embeddings` endpoint).
- **GithubRepositoryReader traverses git tree per-directory** (no recursive API flag). Large monorepos need `filter_directories` exclusion list. `node_modules`, `.git`, `__pycache__`, `dist`, `build`, `venv`, etc. are excluded by default.
- **LanceDB uses `to_pandas()` for dedup** — acceptable for on-premise scale, may need optimisation for 100k+ chunks.

---

## What's Next (Phase 4)

- **Rule lifecycle** — ✅ Contradiction detection (complete), ✅ Expiry detection (complete), ✅ Re-extraction triggers (complete)
- **Skills export** — ✅ `skills.json` format for agent consumption (complete)
- **TUI** — Textual 8-screen terminal interface
- **Licensing** — HMAC license keys, 14-day trial mode
- **Binary distribution** — PyInstaller single-file build

---

## License

All rights reserved @KORE-Labs
