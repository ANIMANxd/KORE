# KORE - On-Premise Context Engine

An AI-powered system that ingests fragmented company data from sources like Slack, Notion, GitHub, and Jira, extracts hidden business rules using LLM reasoning, and outputs structured, validated YAML/JSON skill files that AI agents can enforce reliably.

**Phase**: Phase 12 (Agent Integration) — MCP server, context injector, Claude Code hooks, knowledge graph, and memory store all built. 205 tests, all passing.

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

### 3. Data Ingestion (`ingestion/`)

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

### 4. Extraction Engine (`engine/`)

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

### 5. Knowledge Graph (`graph/`)

Entity-relationship graph built from verified rules:

- **`models.py`** - Pydantic v2 models:
  - `Entity` — entity_id, entity_type (rule/team/process/system/policy/person_role/threshold/concept), name, description, source_rule_ids, mention_count, last_seen
  - `Relationship` — relationship_id, from_entity_id, to_entity_id, relationship_type (governs/involves/depends_on/contradicts/supersedes/applies_to/triggers/owned_by), confidence, source_rule_ids
  - `KnowledgeGraph` — entities dict, relationships list, to_json(), summary(), merge_entity(), get_related_entities()
- **`entity_extractor.py`** - LLM-based entity extraction from business rules. Calls `complete_json()` with conservative extraction prompt. Fuzzy-matches against existing entities (85% threshold). Bumps mention_count on match.
- **`store.py`** - Persistence and queries:
  - `load_graph()` / `save_graph()` — atomic JSON write
  - `get_entity_by_name()` — case-insensitive + fuzzy match
  - `get_related_entities()` — BFS traversal with relationship type filter and max_hops
  - `get_rules_for_entity()` — loads linked BusinessRule objects
  - `search_graph()` — keyword + fuzzy search across names and descriptions
  - `get_most_connected_entities()` — ranks by relationship degree

### 6. Memory Store (`memory/`)

Agent-accessible searchable index from verified rules and high-confidence graph entities:

- **`store.py`** - MemoryEntry (Pydantic v2) with content, entry_type, embedding, confidence, freshness, access_count. MemoryStore container.
  - `build_memory_store()` — loads verified rules + entities with mention_count > 2, generates embeddings, saves atomically
  - `query_memory()` — cosine similarity search, increments access_count
  - `build_context_for_agent()` — formats top-k results as clean context block with token truncation
- **`decay.py`** - Freshness scoring:
  - `calculate_freshness()` — exponential time decay + access boost, clamped [0, 1]
  - `decay_sweep()` — recalculates all entries, saves if changed
  - `get_freshness_summary()` — high/mid/low bucket counts

### 7. Output Layer (`output/`)

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

### 8. Agent Integration (`integrations/`)

- **`mcp/server.py`** - Local MCP server (FastMCP) running on company infrastructure. Zero cloud dependency.
  - `query_company_knowledge(query, max_results)` — similarity search over memory store
  - `get_rule_by_category(category)` — filtered rule listing
  - `check_action_against_rules(proposed_action)` — LLM compliance check with structured JSON output
  - `get_company_context(task_description, max_tokens)` — formatted context block for agent consumption
  - `list_all_rules(category)` — summary list with optional filtering
- **`claude_code.py`** - Claude Code settings generator:
  - `generate_claude_code_config()` — returns MCP server + pre_tool_use hook config
  - `setup_claude_code()` — detects OS (Mac/Windows/Linux), reads existing settings, merges KORE config, writes back
- **`hooks/pre_tool_use.py`** - Pre-flight compliance hook for Claude Code:
  - Receives tool context via stdin, builds action description
  - Checks against company rules via direct memory/LLM imports (no MCP round-trip)
  - Prints violation warnings to stderr, always exits 0 (warn, never block)
- **`context_injector.py`** - Generic agent context export:
  - `generate_system_prompt_injection(task_domain, max_tokens, output_format)` — queries memory, formats as markdown/xml/plain
  - `export_context_file()` — writes context block to disk

### 9. TUI (`ui/tui.py`)

Textual-based 8-screen terminal interface:

- **Dashboard** — Stat cards (chunks, rules, verified, needs_review, entities), vector store info, top entities, rules by category, recent extractions
- **Extract** — Query input with live log output showing pipeline progress, JSON repair notifications, result panel with save/discard
- **Review Queue** — DataTable of `needs_review` rules with expand-on-enter, action bar for approve/reject/edit/skip
- **All Rules** — Filterable DataTable by status/category, detail view on enter, re-extract and delete with confirmation modals
- **Ingest** — Source selector (Slack/GitHub/Notion/Jira), mode toggle (local export vs live API), credential status check, progress log with summary
- **Settings** — Provider config display, test connection buttons (LLM/embedding/vector store), license info panel with days remaining, reconfigure hint
- **Knowledge Graph** — Entity browser (40%) with searchable DataTable grouped by type. Detail panel (60%) with name, type, description, mentions/last_seen, connected rules, related entities with relationship type + confidence (1-hop), 2-hop relationships. Shortcuts: `/`=search, `R`=related rules, `G`=expand hop, `Esc`=back
- **Agent Memory** — Stat cards (total/fresh/stale/last built), memory entry browser with freshness bar, entry detail. Action buttons: Build Memory (`b`), Decay Sweep (`d`), Start MCP Server (`s`). MCP status indicator in header.

Keyboard shortcuts: `q`=quit, `Tab`/`Shift+Tab`=next/prev screen, `?`=help, `Esc`=back

### 10. Full CLI (`run.py`)

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

# Knowledge Graph
python run.py graph-stats    # Show entity counts, types, top connected entities

# Memory Store
python run.py build-memory   # Build memory store from rules + graph
python run.py decay-sweep    # Run freshness decay sweep over memory store

# Agent Integration
python run.py mcp-server --port 3333                      # Start local MCP server
python run.py setup-claude-code --port 3333               # Configure Claude Code integration
python run.py export-context --domain "refund policy"    # Export context block for agents

# TUI
python run.py tui              # Launch Textual terminal interface

# System
python run.py status         # Rich dashboard (providers, chunks, rules, repair stats)
python run.py reset-db       # Drop and recreate vector store
python run.py --verbose ...  # Enable debug logging on any command
```

### 11. Test Suite (`tests/`)

205 tests, all passing. No real LLM calls or network requests in tests — everything mocked.

- **`test_json_repair.py`** (5) - Repair loop, markdown fences, exhaustion, counter
- **`test_chunker.py`** (7) - Slack windowing, GitHub code blocks, generic fallback
- **`test_extractor.py`** (5) - Success, parse_failed, confidence thresholds, meta
- **`test_verifier.py`** (6) - Verified, rejected, contradictions, adjusted text
- **`test_schema.py`** (9) - Pydantic validations, excerpt truncation, YAML round-trip
- **`test_connectors.py`** (33) - All data sources, vector store backends, and document ingestion mocked
- **`test_contradiction_detector.py`** (13) - Rule lifecycle contradiction detection
- **`test_expiry_detector.py`** (15) - Rule staleness detection
- **`test_reextractor.py`** (10) - Rule re-extraction
- **`test_skills_writer.py`** (11) - Skills export
- **`test_graph_store.py`** (23) - Graph persistence, fuzzy search, BFS traversal, rule linking, most connected
- **`test_entity_extractor.py`** (12) - Entity extraction, fuzzy matching, mention bumping
- **`test_memory_store.py`** (23) - Memory entry defaults, build from rules+entities, query ranking, context formatting
- **`test_memory_decay.py`** (15) - Freshness calculation, decay sweep, summary buckets
- **`test_mcp_server.py`** (14) - MCP tools: query knowledge, get rules, check action, get context, list rules
- **`test_claude_code_integration.py`** (12) - Config generation, settings merge, OS detection
- **`test_pre_tool_use_hook.py`** (14) - Action description building, compliance check, violation warnings
- **`test_context_injector.py`** (14) - Markdown/XML/plain formatters, prompt injection, file export

Run: `pytest tests/ -v`

### 12. Test Data (`data/sample/`)

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
| **TUI** | Textual | 8-screen terminal interface |
| **MCP Server** | Anthropic MCP Python SDK (FastMCP) | Local Model Context Protocol server for agent integration |
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
├── graph/                       # Knowledge graph (entities, relationships)
│   ├── __init__.py
│   ├── models.py                # Entity, Relationship, KnowledgeGraph models
│   ├── entity_extractor.py      # LLM-based entity extraction
│   └── store.py                 # Persistence and queries
│
├── memory/                      # Agent memory store
│   ├── __init__.py
│   ├── store.py                 # MemoryEntry, MemoryStore, build/query/context
│   └── decay.py                 # Freshness scoring and decay sweep
│
├── mcp/                         # Model Context Protocol server
│   └── server.py                # FastMCP server with 5 tools
│
├── integrations/                # Agent integrations
│   ├── __init__.py
│   ├── claude_code.py           # Claude Code config generator
│   ├── context_injector.py      # Generic agent context export
│   └── hooks/
│       └── pre_tool_use.py      # Claude Code pre-tool-use hook
│
├── output/                      # Schema validation and persistence
│   ├── __init__.py
│   ├── schema.py                # Pydantic v2 models
│   ├── writer.py                # YAML file writer/loader/updater
│   └── skills_writer.py         # skills.json export for agents
│
├── ui/                          # Textual TUI (8 screens)
│   └── tui.py                   # KoreApp with tabbed screens
│
├── tests/                       # Test suite (205 tests, all passing)
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
│   ├── test_skills_writer.py
│   ├── test_graph_store.py
│   ├── test_entity_extractor.py
│   ├── test_memory_store.py
│   ├── test_memory_decay.py
│   ├── test_mcp_server.py
│   ├── test_claude_code_integration.py
│   ├── test_pre_tool_use_hook.py
│   └── test_context_injector.py
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
- **mcp/server.py is loaded via `importlib.util`** in `run.py` to avoid namespace conflict with the installed `mcp` SDK package.

---

## What's Next

- **Licensing** — HMAC license keys, 14-day trial mode
- **Binary distribution** — PyInstaller single-file build
- **Additional data sources** — Teams, Confluence, Linear, Zendesk, Google Drive

---

## License

All rights reserved @KORE-Labs
