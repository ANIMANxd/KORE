"""Connector and backend tests — all external APIs mocked."""

import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from llama_index.core.schema import Document, TextNode


# ---------------------------------------------------------------------------
# Mock heavy optional dependencies before any storage imports happen
# ---------------------------------------------------------------------------

_pyarrow = types.ModuleType("pyarrow")
_pyarrow.schema = MagicMock(return_value=MagicMock())
_pyarrow.field = MagicMock(return_value=MagicMock())
_pyarrow.string = MagicMock()
_pyarrow.float32 = MagicMock()
_pyarrow.list_ = MagicMock(return_value=MagicMock())
_pyarrow.__version__ = "15.0.0"
sys.modules["pyarrow"] = _pyarrow
sys.modules["lancedb"] = types.ModuleType("lancedb")
sys.modules["psycopg2"] = types.ModuleType("psycopg2")
_psycopg2_extras = types.ModuleType("psycopg2.extras")
_psycopg2_extras.Json = lambda x: x  # simple passthrough mock
_psycopg2_extras.connection = MagicMock()  # for type annotations
sys.modules["psycopg2.extras"] = _psycopg2_extras

# psycopg2.extensions is both an attribute and a submodule
_psycopg2_ext = types.ModuleType("psycopg2.extensions")
_psycopg2_ext.new_type = MagicMock()
_psycopg2_ext.new_array_type = MagicMock()
_psycopg2_ext.register_type = MagicMock()
_psycopg2_ext.connection = MagicMock()  # for type annotations
sys.modules["psycopg2.extensions"] = _psycopg2_ext
sys.modules["psycopg2"].extensions = _psycopg2_ext
sys.modules["pgvector"] = types.ModuleType("pgvector")
sys.modules["pgvector.psycopg2"] = types.ModuleType("pgvector.psycopg2")

# Pre-create mock reader modules so patch() can resolve them
sys.modules["llama_index.readers.github"] = MagicMock()
sys.modules["llama_index.readers.github.repository"] = MagicMock()
sys.modules["llama_index.readers.github.repository.github_client"] = MagicMock()
sys.modules["llama_index.readers.github.issues"] = MagicMock()
sys.modules["llama_index.readers.github.issues.github_client"] = MagicMock()
sys.modules["llama_index.readers.notion"] = MagicMock()
sys.modules["llama_index.readers.jira"] = MagicMock()
sys.modules["llama_index.readers.confluence"] = MagicMock()
sys.modules["llama_index.readers.zendesk"] = MagicMock()
sys.modules["llama_index.readers.google"] = MagicMock()
sys.modules["pypdf"] = MagicMock()
sys.modules["docx"] = MagicMock()


# ---------------------------------------------------------------------------
# 1. Slack live loader — missing token raises helpful error
# ---------------------------------------------------------------------------

class TestSlackLiveLoader:
    def test_load_slack_live_missing_token(self):
        """When SLACK_BOT_TOKEN is missing, _load_slack_live raises RuntimeError."""
        from ingestion.loader import _load_slack_live

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError) as exc_info:
                _load_slack_live()

        msg = str(exc_info.value)
        assert "SLACK_BOT_TOKEN" in msg
        assert "not set" in msg


# ---------------------------------------------------------------------------
# 2–4. GitHub / Notion / Jira — metadata set correctly
# ---------------------------------------------------------------------------

class TestGitHubLoader:
    """Mock GithubRepositoryReader and verify metadata enrichment."""

    @patch("ingestion.loader.os.getenv")
    @patch("llama_index.readers.github.GithubRepositoryReader")
    @patch("llama_index.readers.github.GitHubRepositoryIssuesReader")
    def test_load_github_sets_correct_metadata(
        self, mock_issues_cls, mock_repo_cls, mock_getenv
    ):
        """Mock reader returns docs; assert source_type='github' in metadata."""
        mock_getenv.side_effect = lambda key, default=None: {
            "GITHUB_TOKEN": "ghp_test",
            "GITHUB_REPO": "owner/repo",
            "GITHUB_BRANCH": "main",
            "GITHUB_EXTENSIONS": ".py .md",
        }.get(key, default)

        mock_repo = MagicMock()
        mock_repo.load_data.return_value = [
            Document(
                text="print('hello')",
                metadata={"file_path": "src/main.py", "file_name": "main.py"},
                id_="gh_doc1",
            ),
        ]
        mock_repo_cls.return_value = mock_repo

        mock_issues = MagicMock()
        mock_issues.load_data.return_value = []
        mock_issues_cls.return_value = mock_issues

        from ingestion.loader import _load_github

        docs = _load_github()

        assert len(docs) == 1
        assert docs[0].metadata["source_type"] == "github"
        assert docs[0].metadata["repo"] == "owner/repo"
        assert docs[0].metadata["branch"] == "main"


class TestNotionLoader:
    """Mock NotionPageReader and verify metadata enrichment."""

    @patch("ingestion.loader.os.getenv")
    @patch("llama_index.readers.notion.NotionPageReader")
    def test_load_notion_sets_correct_metadata(self, mock_reader_cls, mock_getenv):
        """Mock reader returns docs; assert source_type='notion' in metadata."""
        mock_getenv.side_effect = lambda key, default=None: {
            "NOTION_TOKEN": "ntn_test",
        }.get(key, default)

        mock_reader = MagicMock()
        mock_reader.load_data.return_value = [
            Document(
                text="Page content here",
                metadata={"page_id": "page-123"},
                id_="page-123",
            ),
        ]
        mock_reader_cls.return_value = mock_reader

        from ingestion.loader import _load_notion

        docs = _load_notion()

        assert len(docs) == 1
        assert docs[0].metadata["source_type"] == "notion"
        assert docs[0].metadata["page_id"] == "page-123"


class TestJiraLoader:
    """Mock JiraReader and verify metadata enrichment."""

    @patch("ingestion.loader.os.getenv")
    @patch("llama_index.readers.jira.JiraReader")
    def test_load_jira_sets_correct_metadata(self, mock_reader_cls, mock_getenv):
        """Mock reader returns docs; assert source_type='jira' and ticket metadata."""
        mock_getenv.side_effect = lambda key, default=None: {
            "JIRA_URL": "https://test.atlassian.net",
            "JIRA_USERNAME": "test@example.com",
            "JIRA_API_TOKEN": "test_token",
            "JIRA_PROJECT_KEYS": "KAN",
        }.get(key, default)

        mock_reader = MagicMock()
        mock_reader.load_data.return_value = [
            Document(
                text="Bug description",
                metadata={"status": "Open", "assignee": "alex"},
                id_="10001",
            ),
        ]
        mock_reader_cls.return_value = mock_reader

        from ingestion.loader import _load_jira

        docs = _load_jira()

        assert len(docs) == 1
        assert docs[0].metadata["source_type"] == "jira"
        assert docs[0].metadata["ticket_id"] == "10001"
        assert docs[0].metadata["status"] == "Open"
        assert docs[0].metadata["assignee"] == "alex"


# ---------------------------------------------------------------------------
# 5. Unsupported source
# ---------------------------------------------------------------------------

class TestLoadRouting:
    def test_unsupported_source_raises(self):
        """Calling load() with an unsupported source raises NotImplementedError."""
        from ingestion.loader import load

        with pytest.raises(NotImplementedError) as exc_info:
            load("unknown_source")

        msg = str(exc_info.value)
        assert "unknown_source" in msg
        assert "slack" in msg or "github" in msg


# ---------------------------------------------------------------------------
# 6–7. Chunker routing per source type
# ---------------------------------------------------------------------------

class TestGitHubChunker:
    def test_github_chunker_used_for_github_docs(self):
        """GitHub docs with H2/H3 headings should split at heading boundaries."""
        from ingestion.chunker import chunk_documents

        doc = Document(
            text="## Setup\nRun pip install.\n\n## Usage\nCall the API.\n\n### Details\nMore info.",
            metadata={"source_type": "github", "title": "README"},
            id_="gh_readme",
        )
        chunks = chunk_documents([doc])

        assert len(chunks) >= 2
        assert all(c.metadata.get("source_type") == "github" for c in chunks)
        setup_chunks = [c for c in chunks if "Setup" in c.text]
        usage_chunks = [c for c in chunks if "Usage" in c.text]
        assert len(setup_chunks) >= 1
        assert len(usage_chunks) >= 1


class TestJiraChunker:
    def test_jira_chunker_used_for_jira_docs(self):
        """Every Jira chunk must carry ticket_id, status, and assignee."""
        from ingestion.chunker import chunk_documents

        doc = Document(
            text="",
            metadata={
                "source_type": "jira",
                "ticket_id": "PROJ-42",
                "status": "In Progress",
                "assignee": "alex.morgan",
                "description": "API returns 500s under load.",
                "comments": [
                    {"text": "I can reproduce.", "author": "jordan"},
                    {"text": "Looking at logs.", "author": "taylor"},
                ],
            },
            id_="jira_proj42",
        )
        chunks = chunk_documents([doc])

        assert len(chunks) > 0
        for c in chunks:
            assert c.metadata.get("ticket_id") == "PROJ-42"
            assert c.metadata.get("status") == "In Progress"
            assert c.metadata.get("assignee") == "alex.morgan"
            assert c.metadata.get("source_type") == "jira"


# ---------------------------------------------------------------------------
# 8. LanceDB backend — store and search
# ---------------------------------------------------------------------------

class TestLanceDBBackend:
    """Test LanceDBBackend with mocked embedder and mocked LanceDB internals."""

    @patch("providers.embedder.embed_text")
    @patch("providers.embedder.embed_batch")
    @patch("providers.config.get_config")
    def test_lancedb_backend_store_and_search(
        self, mock_get_config, mock_embed_batch, mock_embed_text
    ):
        """Store 3 chunks, search, assert results returned."""
        from storage.backends.lancedb_backend import LanceDBBackend

        mock_get_config.return_value = MagicMock(
            embedding_dimensions=4,
            lancedb_data_dir="/tmp/kore_test_lancedb",
        )

        mock_embed_batch.return_value = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
        ]
        mock_embed_text.return_value = [1.0, 0.0, 0.0, 0.0]

        # Mock LanceDB table
        mock_table = MagicMock()
        mock_table.count_rows.return_value = 3
        mock_table.to_pandas.return_value = MagicMock(
            __getitem__=MagicMock(return_value=MagicMock(tolist=MagicMock(return_value=[]))),
            columns=["chunk_id"],
        )
        mock_table.search.return_value.metric.return_value.limit.return_value.to_list.return_value = [
            {"chunk_id": "chunk_1", "content": "hello world", "metadata": '{"source_type":"slack"}', "_distance": 0.1},
        ]

        mock_db = MagicMock()
        mock_db.open_table.return_value = mock_table
        # No table exists initially
        mock_db.table_names.return_value = []

        # lancedb is imported lazily inside _get_db(), so patch sys.modules directly
        sys.modules["lancedb"].connect = MagicMock(return_value=mock_db)

        backend = LanceDBBackend()
        backend.init_db()

        nodes = [
            TextNode(
                text="hello world",
                metadata={"chunk_id": "chunk_1", "source_type": "slack"},
                id_="chunk_1",
            ),
            TextNode(
                text="foo bar",
                metadata={"chunk_id": "chunk_2", "source_type": "github"},
                id_="chunk_2",
            ),
            TextNode(
                text="baz qux",
                metadata={"chunk_id": "chunk_3", "source_type": "jira"},
                id_="chunk_3",
            ),
        ]

        count = backend.store_chunks(nodes)
        assert count == 3

        results = backend.similarity_search("hello", top_k=5)
        assert len(results) >= 1
        assert results[0]["chunk_id"] == "chunk_1"

        # get_chunk_count
        assert backend.get_chunk_count() == 3

        # health_check
        assert backend.health_check() is True


# ---------------------------------------------------------------------------
# 9. pgvector backend — same interface as LanceDB
# ---------------------------------------------------------------------------

class TestPgvectorBackend:
    """Test PgvectorBackend with mocked psycopg2."""

    @patch("storage.backends.pgvector_backend.psycopg2")
    @patch("providers.embedder.embed_text")
    @patch("providers.embedder.embed_batch")
    @patch("storage.backends.pgvector_backend.get_config")
    def test_pgvector_backend_interface(
        self, mock_get_config, mock_embed_batch, mock_embed_text, mock_psycopg2
    ):
        """Mock psycopg2 and verify all interface methods work."""
        from storage.backends.pgvector_backend import PgvectorBackend

        mock_get_config.return_value = MagicMock(
            embedding_dimensions=4,
            pgvector_url="postgresql://test:test@localhost/test",
            embedding_provider="lmstudio",
            embedding_model="test-embed",
            embedding_api_key=None,
            embedding_base_url="http://localhost:1234/v1",
        )

        mock_embed_batch.return_value = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
        ]
        mock_embed_text.return_value = [1.0, 0.0, 0.0, 0.0]

        # Mock connection and cursor
        mock_cur = MagicMock()
        # fetchone returns None for init_db (no existing table), then real values later
        mock_cur.fetchone.side_effect = [
            [False],        # init_db: table_exists check
            [42],           # get_chunk_count
            [1],            # health_check
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_psycopg2.connect.return_value = mock_conn

        backend = PgvectorBackend()
        backend.init_db()

        # store_chunks
        nodes = [
            TextNode(
                text="test doc",
                metadata={"chunk_id": "pg_1"},
                id_="pg_1",
            ),
        ]
        count = backend.store_chunks(nodes)
        assert count == 1

        # get_chunk_count
        assert backend.get_chunk_count() == 42

        # health_check
        assert backend.health_check() is True

        # Verify connection was closed at least once
        assert mock_conn.close.called


# ---------------------------------------------------------------------------
# 10. Backend switch via config
# ---------------------------------------------------------------------------

class TestBackendSwitch:
    """Changing config backend should instantiate the correct class."""

    @patch("storage.backends.lancedb_backend.LanceDBBackend")
    @patch("storage.backends.pgvector_backend.PgvectorBackend")
    @patch("providers.config.get_config")
    def test_backend_switch_via_config(
        self, mock_get_config, mock_pgvector_cls, mock_lancedb_cls
    ):
        """When config says 'lancedb', LanceDBBackend is used. When 'pgvector', PgvectorBackend."""
        from storage.vector_store import _get_backend

        import storage.vector_store as vs
        vs._backend_instance = None

        # Test LanceDB
        mock_get_config.return_value = MagicMock(vector_store_backend="lancedb")
        _get_backend()
        mock_lancedb_cls.assert_called_once()
        mock_pgvector_cls.assert_not_called()

        # Reset singleton
        vs._backend_instance = None
        mock_lancedb_cls.reset_mock()
        mock_pgvector_cls.reset_mock()

        # Test pgvector
        mock_get_config.return_value = MagicMock(vector_store_backend="pgvector")
        _get_backend()
        mock_pgvector_cls.assert_called_once()
        mock_lancedb_cls.assert_not_called()

        # Reset singleton for other tests
        vs._backend_instance = None

    @patch("providers.config.get_config")
    def test_backend_switch_invalid_raises(self, mock_get_config):
        """Invalid backend name raises ValueError with helpful message."""
        from storage.vector_store import _get_backend

        import storage.vector_store as vs
        vs._backend_instance = None

        mock_get_config.return_value = MagicMock(vector_store_backend="mongodb")

        with pytest.raises(ValueError) as exc_info:
            _get_backend()

        msg = str(exc_info.value)
        assert "mongodb" in msg
        assert "lancedb" in msg
        assert "pgvector" in msg

        vs._backend_instance = None


# ---------------------------------------------------------------------------
# Extended connector tests (Teams, Confluence, Linear, Zendesk, Google Drive, Documents)
# ---------------------------------------------------------------------------


class TestTeamsConnector:
    """Microsoft Teams loader — mocked OAuth2 and Graph API."""

    @patch("ingestion.loader.os.getenv")
    @patch("click.prompt")
    @patch("ingestion.loader._write_teams_env")
    def test_teams_missing_credentials_prompts_setup(
        self, mock_write_env, mock_prompt, mock_getenv
    ):
        """When env vars are missing, interactive setup runs and writes .env."""
        mock_getenv.side_effect = lambda key, default=None: None
        mock_prompt.side_effect = ["client_id", "client_secret", "tenant_id", "team1"]

        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={"access_token": "token123"}),
            )
            with patch("requests.get") as mock_get:
                mock_get.return_value = MagicMock(
                    status_code=200,
                    json=MagicMock(
                        return_value={
                            "displayName": "Team Alpha",
                            "value": [],
                        }
                    ),
                )
                with patch("rich.console.Console"):
                    with patch("rich.progress.Progress"):
                        from ingestion.loader import _load_teams

                        _load_teams()

        assert mock_prompt.call_count >= 4
        mock_write_env.assert_called_once()

    @patch("ingestion.loader.os.getenv")
    @patch("requests.post")
    @patch("requests.get")
    def test_teams_sets_correct_metadata(self, mock_get, mock_post, mock_getenv):
        """Mocked Graph API returns messages with proper metadata fields."""
        mock_getenv.side_effect = lambda key, default=None: {
            "TEAMS_CLIENT_ID": "c1",
            "TEAMS_CLIENT_SECRET": "s1",
            "TEAMS_TENANT_ID": "t1",
            "TEAMS_TEAM_IDS": "team1",
        }.get(key, default)

        mock_post.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"access_token": "tok"}),
        )

        mock_get.side_effect = [
            MagicMock(
                status_code=200,
                json=MagicMock(return_value={"displayName": "Team Alpha"}),
            ),
            MagicMock(
                status_code=200,
                json=MagicMock(
                    return_value={
                        "value": [{"id": "ch1", "displayName": "General"}],
                    }
                ),
            ),
            MagicMock(
                status_code=200,
                json=MagicMock(
                    return_value={
                        "value": [
                            {
                                "id": "msg1",
                                "body": {"content": "Hello team"},
                                "from": {"user": {"id": "u1", "displayName": "Alice"}},
                                "createdDateTime": "2024-01-01T10:00:00Z",
                                "messageType": "message",
                            }
                        ],
                        "@odata.nextLink": None,
                    }
                ),
            ),
        ]

        with patch("rich.console.Console"):
            from ingestion.loader import _load_teams

            docs = _load_teams()

        assert len(docs) == 1
        meta = docs[0].metadata
        assert meta["source_type"] == "teams"
        assert meta["team_id"] == "team1"
        assert meta["team_name"] == "Team Alpha"
        assert meta["channel_name"] == "General"
        assert meta["timestamp"] == "2024-01-01T10:00:00Z"
        assert meta["user_id"] == "u1"
        assert meta["user_name"] == "Alice"

    def test_teams_chunker_uses_time_window(self):
        """TeamsChunker groups messages within 15-minute windows."""
        from datetime import datetime, timedelta, timezone

        from ingestion.chunker import chunk_documents

        base = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        docs = [
            Document(
                text="msg1",
                metadata={
                    "source_type": "teams",
                    "timestamp": base.isoformat(),
                    "team_id": "t1",
                    "channel_name": "general",
                },
                id_="t1",
            ),
            Document(
                text="msg2",
                metadata={
                    "source_type": "teams",
                    "timestamp": (base + timedelta(minutes=5)).isoformat(),
                    "team_id": "t1",
                    "channel_name": "general",
                },
                id_="t2",
            ),
            Document(
                text="msg3",
                metadata={
                    "source_type": "teams",
                    "timestamp": (base + timedelta(minutes=21)).isoformat(),
                    "team_id": "t1",
                    "channel_name": "general",
                },
                id_="t3",
            ),
        ]
        chunks = chunk_documents(docs)
        assert len(chunks) == 2
        assert all(c.metadata.get("source_type") == "teams" for c in chunks)
        assert all(c.metadata.get("team_id") == "t1" for c in chunks)
        assert all(c.metadata.get("channel_name") == "general" for c in chunks)


class TestConfluenceConnector:
    """Confluence loader — mocked ConfluenceReader from LlamaHub."""

    @patch("ingestion.loader.os.getenv")
    @patch("click.prompt")
    @patch("ingestion.loader._write_confluence_env")
    def test_confluence_missing_credentials_prompts_setup(
        self, mock_write_env, mock_prompt, mock_getenv
    ):
        """When env vars are missing, interactive setup runs and writes .env."""
        mock_getenv.side_effect = lambda key, default=None: None
        mock_prompt.side_effect = [
            "https://test.atlassian.net/wiki",
            "test@test.com",
            "token",
            "PROJ",
        ]

        with patch("llama_index.readers.confluence.ConfluenceReader") as mock_reader_cls:
            mock_reader = MagicMock()
            mock_reader.load_data.return_value = []
            mock_reader_cls.return_value = mock_reader
            with patch("rich.console.Console"):
                from ingestion.loader import _load_confluence

                _load_confluence()

        assert mock_prompt.call_count >= 4
        mock_write_env.assert_called_once()

    @patch("ingestion.loader.os.getenv")
    @patch("llama_index.readers.confluence.ConfluenceReader")
    def test_confluence_sets_correct_metadata(self, mock_reader_cls, mock_getenv):
        """ConfluenceReader docs are enriched with source_type and space metadata."""
        mock_getenv.side_effect = lambda key, default=None: {
            "CONFLUENCE_URL": "https://test.atlassian.net/wiki",
            "CONFLUENCE_USERNAME": "test@test.com",
            "CONFLUENCE_API_TOKEN": "token",
            "CONFLUENCE_SPACE_KEYS": "PROJ",
        }.get(key, default)

        mock_reader = MagicMock()
        mock_reader.load_data.return_value = [
            Document(
                text="Page content here",
                metadata={"title": "Home"},
                id_="page-1",
            ),
        ]
        mock_reader_cls.return_value = mock_reader

        with patch("rich.console.Console"):
            from ingestion.loader import _load_confluence

            docs = _load_confluence()

        assert len(docs) == 1
        meta = docs[0].metadata
        assert meta["source_type"] == "confluence"
        assert meta["space_key"] == "PROJ"
        assert meta["page_title"] == "Home"
        assert meta["page_id"] == "page-1"

    def test_confluence_chunker_respects_headings(self):
        """ConfluenceChunker splits at heading boundaries."""
        from ingestion.chunker import chunk_documents

        doc = Document(
            text="# Title\n\nIntro paragraph.\n\n## Section 1\n\nDetails here.\n\n## Section 2\n\nMore details.",
            metadata={
                "source_type": "confluence",
                "space_key": "PROJ",
                "page_title": "Guide",
            },
            id_="conf_1",
        )
        chunks = chunk_documents([doc])
        assert len(chunks) >= 2
        assert all(c.metadata.get("space_key") == "PROJ" for c in chunks)
        assert all(c.metadata.get("page_title") == "Guide" for c in chunks)


class TestLinearConnector:
    """Linear loader — mocked GraphQL API."""

    @patch("ingestion.loader.os.getenv")
    @patch("requests.post")
    def test_linear_sets_correct_metadata(self, mock_post, mock_getenv):
        """Mocked GraphQL responses produce documents with Linear metadata."""
        mock_getenv.side_effect = lambda key, default=None: {
            "LINEAR_API_KEY": "lin_key",
            "LINEAR_TEAM_NAMES": "",
        }.get(key, default)

        mock_post.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(
                return_value={
                    "data": {
                        "teams": {
                            "nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]
                        },
                        "issues": {
                            "nodes": [
                                {
                                    "id": "iss1",
                                    "identifier": "ENG-1",
                                    "title": "Bug",
                                    "description": "Desc",
                                    "state": {"name": "Done"},
                                    "assignee": {"name": "Alice", "email": "a@example.com"},
                                    "comments": {"nodes": []},
                                    "project": None,
                                    "createdAt": "2024-01-01T00:00:00Z",
                                    "updatedAt": "2024-01-02T00:00:00Z",
                                }
                            ]
                        },
                        "projects": {"nodes": []},
                        "documents": {"nodes": []},
                    }
                }
            ),
        )

        with patch("rich.console.Console"):
            from ingestion.loader import _load_linear

            docs = _load_linear()

        issue_docs = [d for d in docs if d.metadata.get("issue_id")]
        assert len(issue_docs) == 1
        meta = issue_docs[0].metadata
        assert meta["source_type"] == "linear"
        assert meta["team_name"] == "Engineering"
        assert meta["team_key"] == "ENG"
        assert meta["issue_id"] == "ENG-1"
        assert meta["status"] == "Done"
        assert meta["assignee"] == "Alice"

    def test_linear_chunker_includes_issue_id(self):
        """LinearChunker preserves issue_id and team_name in every chunk."""
        from ingestion.chunker import chunk_documents

        doc = Document(
            text="Bug description\n\nMore details",
            metadata={
                "source_type": "linear",
                "issue_id": "ENG-42",
                "team_name": "Engineering",
                "status": "In Progress",
                "assignee": "Alice",
                "comments": [{"text": "Confirmed", "author": "Bob"}],
            },
            id_="lin_1",
        )
        chunks = chunk_documents([doc])
        assert len(chunks) > 0
        for c in chunks:
            assert c.metadata.get("issue_id") == "ENG-42"
            assert c.metadata.get("team_name") == "Engineering"


class TestZendeskConnector:
    """Zendesk loader — mocked REST API and ZendeskReader."""

    @patch("ingestion.loader.os.getenv")
    @patch("requests.get")
    @patch("llama_index.readers.zendesk.ZendeskReader")
    def test_zendesk_sets_doc_type_in_metadata(
        self, mock_reader_cls, mock_get, mock_getenv
    ):
        """Zendesk docs carry doc_type (macro/ticket/article) in metadata."""
        mock_getenv.side_effect = lambda key, default=None: {
            "ZENDESK_SUBDOMAIN": "test",
            "ZENDESK_EMAIL": "test@test.com",
            "ZENDESK_API_TOKEN": "token",
        }.get(key, default)

        # Macros endpoint
        def _mock_get(path, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "macros" in path:
                resp.json = MagicMock(
                    return_value={
                        "macros": [
                            {"id": 1, "title": "Macro1", "actions": [{"field": "priority", "value": "high"}]}
                        ]
                    }
                )
            elif "tickets" in path:
                resp.json = MagicMock(return_value={"tickets": []})
            elif "comments" in path:
                resp.json = MagicMock(return_value={"comments": []})
            else:
                resp.json = MagicMock(return_value={})
            return resp

        mock_get.side_effect = _mock_get

        mock_reader = MagicMock()
        mock_reader.load_data.return_value = [
            Document(
                text="Article body",
                metadata={"title": "Help"},
                id_="art-1",
            ),
        ]
        mock_reader_cls.return_value = mock_reader

        with patch("rich.console.Console"):
            from ingestion.loader import _load_zendesk

            docs = _load_zendesk()

        macro_docs = [d for d in docs if d.metadata.get("doc_type") == "macro"]
        article_docs = [d for d in docs if d.metadata.get("doc_type") == "article"]
        assert len(macro_docs) == 1
        assert len(article_docs) == 1
        assert macro_docs[0].metadata["macro_id"] == "1"
        assert article_docs[0].metadata["article_id"] == "art-1"

    def test_zendesk_macro_gets_single_chunk(self):
        """Zendesk macro documents produce exactly one chunk."""
        from ingestion.chunker import chunk_documents

        doc = Document(
            text="Macro: Set priority\nfield: priority\nvalue: high",
            metadata={
                "source_type": "zendesk",
                "doc_type": "macro",
                "macro_id": "123",
                "title": "Priority High",
            },
            id_="zd_macro_1",
        )
        chunks = chunk_documents([doc])
        assert len(chunks) == 1
        assert chunks[0].metadata["chunk_type"] == "macro"

    def test_zendesk_ticket_splits_comments(self):
        """Zendesk ticket document splits body and comments into separate chunks."""
        from ingestion.chunker import chunk_documents

        doc = Document(
            text="Ticket 42: Subject\n\nDescription here",
            metadata={
                "source_type": "zendesk",
                "doc_type": "ticket",
                "ticket_id": "42",
                "status": "open",
                "assignee_id": "u1",
                "comments": [
                    {"text": "First comment", "author_id": "u2"},
                    {"text": "Second comment", "author_id": "u3"},
                ],
            },
            id_="zd_ticket_1",
        )
        chunks = chunk_documents([doc])
        body_chunks = [c for c in chunks if c.metadata.get("chunk_type") == "body"]
        comment_chunks = [c for c in chunks if c.metadata.get("chunk_type") == "comment"]
        assert len(body_chunks) == 1
        assert len(comment_chunks) == 2
        assert comment_chunks[0].metadata["ticket_id"] == "42"
        assert comment_chunks[1].metadata["ticket_id"] == "42"


class TestGoogleDriveConnector:
    """Google Drive loader — mocked GoogleDriveReader from LlamaHub."""

    @patch("ingestion.loader.os.getenv")
    @patch("llama_index.readers.google.GoogleDriveReader")
    def test_google_drive_sets_correct_metadata(self, mock_reader_cls, mock_getenv):
        """GoogleDriveReader docs are enriched with file metadata."""
        mock_getenv.side_effect = lambda key, default=None: {
            "GOOGLE_DRIVE_CREDENTIALS_FILE": "/tmp/creds.json",
            "GOOGLE_DRIVE_FOLDER_IDS": "folder1",
        }.get(key, default)

        mock_reader = MagicMock()
        mock_reader.load_data.return_value = [
            Document(
                text="Doc content",
                metadata={"file_name": "report.pdf", "mime_type": "application/pdf"},
                id_="file-1",
            ),
        ]
        mock_reader_cls.return_value = mock_reader

        with patch("ingestion.loader.Path") as MockPath:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = '{"type": "service_account"}'
            MockPath.return_value = mock_path
            with patch("rich.console.Console"):
                from ingestion.loader import _load_google_drive

                docs = _load_google_drive()

        assert len(docs) == 1
        meta = docs[0].metadata
        assert meta["source_type"] == "google_drive"
        assert meta["file_id"] == "file-1"
        assert meta["file_name"] == "report.pdf"
        assert meta["mime_type"] == "application/pdf"

    @patch("ingestion.loader.os.getenv")
    @patch("click.prompt")
    @patch("ingestion.loader._write_google_drive_env")
    def test_google_drive_handles_missing_credentials(
        self, mock_write_env, mock_prompt, mock_getenv
    ):
        """When credentials are missing, interactive setup prompts for values."""
        mock_getenv.side_effect = lambda key, default=None: None
        mock_prompt.side_effect = ["/tmp/creds.json", "folder1"]

        with patch("llama_index.readers.google.GoogleDriveReader") as mock_reader_cls:
            mock_reader = MagicMock()
            mock_reader.load_data.return_value = []
            mock_reader_cls.return_value = mock_reader
            with patch("ingestion.loader.Path") as MockPath:
                mock_path = MagicMock()
                mock_path.exists.return_value = True
                mock_path.read_text.return_value = "{}"
                MockPath.return_value = mock_path
                with patch("rich.console.Console"):
                    from ingestion.loader import _load_google_drive

                    _load_google_drive()

        assert mock_prompt.call_count >= 2
        mock_write_env.assert_called_once()


class TestDocumentLoader:
    """Local document ingestion — mocked filesystem and extraction libraries."""

    def _mock_file_path(self, path_str, suffix, size=1024):
        mock = MagicMock()
        mock.exists.return_value = True
        mock.is_file.return_value = True
        mock.suffix = suffix
        mock.name = os.path.basename(path_str)
        mock.stem = os.path.splitext(mock.name)[0]
        mock.stat.return_value = MagicMock(st_size=size, st_mtime=1700000000)
        mock.parts = tuple(path_str.split("/"))
        return mock

    def _mock_dir_path(self, path_str, rglob_results):
        mock = MagicMock()
        mock.exists.return_value = True
        mock.is_file.return_value = False
        mock.rglob.return_value = rglob_results
        mock.name = os.path.basename(path_str)
        return mock

    @patch("ingestion.loader.Path")
    @patch("pypdf.PdfReader")
    @patch("rich.console.Console")
    def test_pdf_extraction_returns_documents(self, mock_console, mock_pdf, mock_path_cls):
        """PDF files are extracted with pypdf and returned as Documents."""
        mock_file = self._mock_file_path("/tmp/test.pdf", ".pdf", 2048)
        mock_path_cls.return_value = mock_file

        mock_reader = MagicMock()
        mock_reader.pages = [
            MagicMock(extract_text=MagicMock(return_value="Page 1 text")),
            MagicMock(extract_text=MagicMock(return_value="Page 2 text")),
        ]
        mock_pdf.return_value = mock_reader

        from ingestion.loader import _load_documents

        docs = _load_documents("/tmp/test.pdf")

        assert len(docs) == 1
        assert docs[0].metadata["file_type"] == "pdf"
        assert docs[0].metadata["page_count"] == 2
        assert docs[0].metadata["source_type"] == "document"

    @patch("ingestion.loader.Path")
    @patch("docx.Document")
    @patch("rich.console.Console")
    def test_docx_extraction_returns_documents(self, mock_console, mock_docx, mock_path_cls):
        """Word documents are extracted with python-docx and returned as Documents."""
        mock_file = self._mock_file_path("/tmp/test.docx", ".docx", 1024)
        mock_path_cls.return_value = mock_file

        mock_doc = MagicMock()
        mock_para = MagicMock()
        mock_para.text = "Paragraph 1"
        mock_doc.paragraphs = [mock_para]
        mock_doc.tables = []
        mock_docx.return_value = mock_doc

        from ingestion.loader import _load_documents

        docs = _load_documents("/tmp/test.docx")

        assert len(docs) == 1
        assert docs[0].metadata["file_type"] == "docx"
        assert docs[0].metadata["source_type"] == "document"

    @patch("ingestion.loader.Path")
    @patch("rich.console.Console")
    def test_markdown_read_correctly(self, mock_console, mock_path_cls):
        """Markdown files are read directly and returned as Documents."""
        mock_file = self._mock_file_path("/tmp/readme.md", ".md", 512)
        mock_file.read_text.return_value = "# Heading\n\nContent here."
        mock_path_cls.return_value = mock_file

        from ingestion.loader import _load_documents

        docs = _load_documents("/tmp/readme.md")

        assert len(docs) == 1
        assert docs[0].metadata["file_type"] == "markdown"
        assert "# Heading" in docs[0].text

    @patch("ingestion.loader.Path")
    @patch("rich.console.Console")
    def test_unsupported_file_type_skipped(self, mock_console, mock_path_cls):
        """Unsupported file types are skipped with a warning indicator."""
        mock_file = self._mock_file_path("/tmp/chart.png", ".png", 256)
        mock_path_cls.return_value = mock_file

        from ingestion.loader import _load_documents

        docs = _load_documents("/tmp/chart.png")

        assert len(docs) == 0

    @patch("ingestion.loader.Path")
    @patch("rich.console.Console")
    def test_recursive_directory_scan(self, mock_console, mock_path_cls):
        """Directory scan recursively finds supported files and skips excluded dirs."""
        md_file = self._mock_file_path("/tmp/docs/readme.md", ".md", 100)
        md_file.read_text.return_value = "Readme"
        txt_file = self._mock_file_path("/tmp/docs/notes.txt", ".txt", 100)
        txt_file.read_text.return_value = "Notes"
        node_module_file = self._mock_file_path("/tmp/docs/node_modules/pkg/index.md", ".md", 100)
        node_module_file.read_text.return_value = "Should be skipped"

        mock_root = self._mock_dir_path("/tmp/docs", [md_file, txt_file, node_module_file])
        mock_path_cls.return_value = mock_root

        from ingestion.loader import _load_documents

        docs = _load_documents("/tmp/docs")

        # node_modules should be skipped due to EXCLUDED_DIRS check
        assert len(docs) == 2
        file_names = {d.metadata["file_name"] for d in docs}
        assert "readme.md" in file_names
        assert "notes.txt" in file_names

    @patch("ingestion.loader.Path")
    @patch("pypdf.PdfReader")
    def test_image_only_pdf_skipped_with_warning(self, mock_pdf, mock_path_cls):
        """Image-only PDFs (no extractable text) are skipped with a warning."""
        mock_file = self._mock_file_path("/tmp/image_only.pdf", ".pdf", 1024)
        mock_path_cls.return_value = mock_file

        mock_reader = MagicMock()
        mock_reader.pages = [
            MagicMock(extract_text=MagicMock(return_value="")),
        ]
        mock_pdf.return_value = mock_reader

        mock_console = MagicMock()
        mock_console_class = MagicMock(return_value=mock_console)

        with patch("rich.console.Console", mock_console_class):
            from ingestion.loader import _load_documents

            docs = _load_documents("/tmp/image_only.pdf")

        assert len(docs) == 0
        print_calls = [c for c in mock_console.print.call_args_list if "image-only" in str(c)]
        assert len(print_calls) >= 1

    @patch("ingestion.loader.Path")
    @patch("rich.console.Console")
    def test_likely_explicit_policy_flag_set_on_policy_files(self, mock_console, mock_path_cls):
        """Filenames containing policy keywords set likely_explicit_policy=True."""
        policy_file = self._mock_file_path("/tmp/refund-policy.md", ".md", 100)
        policy_file.read_text.return_value = "Policy text"
        other_file = self._mock_file_path("/tmp/notes.txt", ".txt", 100)
        other_file.read_text.return_value = "Notes text"

        mock_root = self._mock_dir_path("/tmp", [policy_file, other_file])
        mock_path_cls.return_value = mock_root

        from ingestion.loader import _load_documents

        docs = _load_documents("/tmp")

        policy_doc = [d for d in docs if d.metadata["file_name"] == "refund-policy.md"][0]
        other_doc = [d for d in docs if d.metadata["file_name"] == "notes.txt"][0]
        assert policy_doc.metadata["likely_explicit_policy"] is True
        assert other_doc.metadata["likely_explicit_policy"] is False

    def test_chunker_routes_pdf_by_page(self):
        """DocumentChunker splits PDFs by page markers."""
        from ingestion.chunker import chunk_documents

        doc = Document(
            text="--- Page 1 ---\nFirst page text\n\n--- Page 2 ---\nSecond page text",
            metadata={
                "source_type": "document",
                "file_type": "pdf",
                "file_name": "test.pdf",
            },
            id_="doc_pdf_1",
        )
        chunks = chunk_documents([doc])
        assert len(chunks) >= 2
        page_numbers = [c.metadata.get("page_number") for c in chunks if c.metadata.get("page_number")]
        assert any("1" in str(p) for p in page_numbers)

    def test_chunker_routes_docx_by_heading(self):
        """DocumentChunker splits DOCX files at heading boundaries."""
        from ingestion.chunker import chunk_documents

        doc = Document(
            text="# Title\n\nParagraph 1\n\n## Section A\n\nParagraph 2\n\n## Section B\n\nParagraph 3",
            metadata={
                "source_type": "document",
                "file_type": "docx",
                "file_name": "test.docx",
            },
            id_="doc_docx_1",
        )
        chunks = chunk_documents([doc])
        assert len(chunks) >= 2
        for c in chunks:
            assert c.metadata.get("file_name") == "test.docx"
            assert c.metadata.get("file_type") == "docx"
