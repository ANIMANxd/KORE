import pytest
from llama_index.core.schema import Document, TextNode

from ingestion.chunker import (
    SlackChunker,
    GitHubChunker,
    GenericChunker,
    chunk_documents,
)


class TestSlackChunker:
    """15-minute time-window chunking for Slack messages."""

    def test_messages_within_window_grouped(self):
        """Messages within 15 minutes should form one chunk."""
        docs = [
            Document(text="Hello", metadata={"source_type": "slack", "timestamp": "2024-01-01T10:00:00Z", "channel": "#general", "user_id": "U1"}),
            Document(text="World", metadata={"source_type": "slack", "timestamp": "2024-01-01T10:05:00Z", "channel": "#general", "user_id": "U2"}),
            Document(text="Again", metadata={"source_type": "slack", "timestamp": "2024-01-01T10:14:00Z", "channel": "#general", "user_id": "U1"}),
        ]
        chunker = SlackChunker()
        chunks = chunker.chunk(docs)

        assert len(chunks) == 1
        assert chunks[0].metadata["message_count"] == 3
        assert "Hello" in chunks[0].text
        assert "World" in chunks[0].text
        assert "Again" in chunks[0].text

    def test_messages_beyond_window_separate(self):
        """Messages more than 15 minutes apart should be separate chunks."""
        docs = [
            Document(text="First", metadata={"source_type": "slack", "timestamp": "2024-01-01T10:00:00Z", "channel": "#general", "user_id": "U1"}),
            Document(text="Second", metadata={"source_type": "slack", "timestamp": "2024-01-01T10:20:00Z", "channel": "#general", "user_id": "U2"}),
        ]
        chunker = SlackChunker()
        chunks = chunker.chunk(docs)

        assert len(chunks) == 2
        assert chunks[0].metadata["message_count"] == 1
        assert chunks[1].metadata["message_count"] == 1

    def test_chunk_has_source_type(self):
        """Every chunk must carry source_type in metadata."""
        docs = [
            Document(text="Hi", metadata={"source_type": "slack", "timestamp": "2024-01-01T10:00:00Z", "channel": "#general", "user_id": "U1"}),
        ]
        chunks = chunk_documents(docs)
        assert all(c.metadata.get("source_type") == "slack" for c in chunks)


class TestGitHubChunker:
    """Markdown + diff-aware chunking."""

    def test_code_block_never_split(self):
        """A code block must remain intact — never split mid-block."""
        text = "## Setup\nRun this:\n```python\nprint('hello')\nprint('world')\n```\n## Usage\nDone."
        doc = Document(text=text, metadata={"source_type": "github", "title": "README"})
        chunker = GitHubChunker()
        chunks = chunker.chunk([doc])

        # Find the chunk containing the code block
        code_chunks = [c for c in chunks if "```python" in c.text]
        assert len(code_chunks) >= 1
        for cc in code_chunks:
            # The ```python and closing ``` must both be present
            assert "```python" in cc.text
            assert "```" in cc.text
            # It shouldn't be split mid-block
            assert cc.text.count("```") >= 2

    def test_heading_boundary_split(self):
        """Chunks should split at H2/H3 headings."""
        text = "## Section A\nContent A.\n## Section B\nContent B."
        doc = Document(text=text, metadata={"source_type": "github", "title": "Doc"})
        chunker = GitHubChunker()
        chunks = chunker.chunk([doc])

        assert len(chunks) >= 2
        assert any("Section A" in c.text for c in chunks)
        assert any("Section B" in c.text for c in chunks)


class TestGenericChunker:
    """Fallback chunking for unknown source types."""

    def test_fallback_used_for_unknown_source(self):
        """Unknown source_type should route to GenericChunker."""
        text = "This is a long text about machine learning and artificial intelligence. " * 50
        doc = Document(
            text=text,
            metadata={"source_type": "unknown"},
        )
        chunks = chunk_documents([doc])

        assert len(chunks) >= 1
        assert all(c.metadata.get("source_type") == "unknown" for c in chunks)
        assert all("chunk_index" in c.metadata for c in chunks)
        assert all("total_chunks" in c.metadata for c in chunks)

    def test_all_chunks_have_source_type(self):
        """Every chunk from every strategy must include source_type."""
        docs = [
            Document(text="A", metadata={"source_type": "slack", "timestamp": "2024-01-01T10:00:00Z", "channel": "#general", "user_id": "U1"}),
            Document(text="B", metadata={"source_type": "github", "title": "README"}),
            Document(text="C" * 500, metadata={"source_type": "notion", "title": "Page"}),
        ]
        chunks = chunk_documents(docs)
        assert len(chunks) > 0
        for c in chunks:
            assert "source_type" in c.metadata, f"Missing source_type in chunk: {c.metadata}"
