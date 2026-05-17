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
    @patch("providers.config.get_config")
    def test_pgvector_backend_interface(
        self, mock_get_config, mock_embed_batch, mock_embed_text, mock_psycopg2
    ):
        """Mock psycopg2 and verify all interface methods work."""
        from storage.backends.pgvector_backend import PgvectorBackend

        mock_get_config.return_value = MagicMock(
            embedding_dimensions=4,
            pgvector_url="postgresql://test:test@localhost/test",
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
