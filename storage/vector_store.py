"""
PostgreSQL + pgvector storage layer.

All embedding calls go through providers/embedder.py.
All config comes from providers/config.py.
"""

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Add project root to path for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
from psycopg2.extras import Json

from llama_index.core.schema import TextNode

# Ensure .env is loaded before any config read
from dotenv import load_dotenv
load_dotenv()

from providers.config import get_config
from providers.embedder import embed_text, embed_batch


SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.75"))
_RETRY_ATTEMPTS = 3


def _get_connection_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL not set. Add it to your .env file, e.g.:\n"
            'DATABASE_URL=postgresql://postgres:animanxd@localhost:5432/kore'
        )
    return url


def _connect() -> psycopg2.extensions.connection:
    url = _get_connection_url()
    last_error: Exception | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            conn = psycopg2.connect(url)
            return conn
        except Exception as exc:
            last_error = exc
            if attempt < _RETRY_ATTEMPTS:
                sleep_seconds = 2 ** attempt
                print(f"[KORE] DB connection failed (attempt {attempt}/{_RETRY_ATTEMPTS}). "
                      f"Retrying in {sleep_seconds}s...")
                time.sleep(sleep_seconds)
            else:
                raise
    raise last_error or RuntimeError("Database connection failed")


def _register_vector(conn: psycopg2.extensions.connection) -> None:
    """Register pgvector vector type with psycopg2."""
    try:
        from pgvector.psycopg2 import register_vector
        register_vector(conn)
    except ImportError:
        # Manual registration if pgvector package not available
        from psycopg2.extensions import new_type, new_array_type, register_type
        vector_type = new_type((16384,), 'VECTOR', lambda value, cursor: value)
        register_type(vector_type, conn)


def _get_existing_vector_dim(conn: psycopg2.extensions.connection) -> int | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT atttypmod
            FROM pg_attribute
            WHERE attrelid = 'document_chunks'::regclass
              AND attname = 'embedding'
              AND atttypid = 'vector'::regtype;
        """)
        row = cur.fetchone()
        if row and row[0] is not None and row[0] > 0:
            return row[0]
    return None


def init_db() -> None:
    """Create the document_chunks table if it does not exist.

    Validates that the existing table's vector dimension matches the current
    config. If not, prints a clear warning and exits.
    """
    config = get_config()
    expected_dim = config.embedding_dimensions

    conn = _connect()
    try:
        # Ensure pgvector extension exists
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            conn.commit()

        # Check if table already exists
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM pg_tables
                    WHERE schemaname = 'public'
                      AND tablename = 'document_chunks'
                );
            """)
            table_exists = cur.fetchone()[0]

        if table_exists:
            existing_dim = _get_existing_vector_dim(conn)
            if existing_dim is not None and existing_dim != expected_dim:
                print(
                    f"[KORE] ERROR: Embedding dimensions changed.\n"
                    f"  Table 'document_chunks' expects vector({existing_dim}),\n"
                    f"  but provider.config.yaml specifies {expected_dim}.\n"
                    f"  Run 'python run.py reset-db' to reinitialise."
                )
                raise SystemExit(1)
            print("[KORE] document_chunks table already exists.")
            return

        # Create table
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE document_chunks (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    content TEXT NOT NULL,
                    embedding VECTOR({expected_dim}),
                    metadata JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            # Index for similarity search
            cur.execute("""
                CREATE INDEX idx_document_chunks_embedding
                ON document_chunks USING ivfflat (embedding vector_cosine_ops);
            """)
            conn.commit()
        print(f"[KORE] Created document_chunks table with vector({expected_dim}).")
    finally:
        conn.close()


def store_chunks(nodes: list[TextNode]) -> int:
    """Embed and store TextNode chunks in pgvector.

    Skips duplicates by chunk_id stored in metadata.

    Returns
    -------
    int
        Number of newly stored chunks.
    """
    if not nodes:
        return 0

    config = get_config()
    conn = _connect()
    _register_vector(conn)

    try:
        # Extract texts and chunk_ids
        texts = [node.text for node in nodes]
        chunk_ids = [node.id_ for node in nodes]

        # Check which chunks already exist
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata->>'chunk_id' FROM document_chunks "
                "WHERE metadata->>'chunk_id' = ANY(%s);",
                (chunk_ids,),
            )
            existing = {row[0] for row in cur.fetchall()}

        to_insert = [(i, node) for i, node in enumerate(nodes) if node.id_ not in existing]

        if not to_insert:
            print("[KORE] All chunks already stored. Nothing new to insert.")
            return 0

        # Embed the texts we need to insert
        indices_to_embed = [idx for idx, _ in to_insert]
        texts_to_embed = [texts[idx] for idx in indices_to_embed]
        embeddings = embed_batch(texts_to_embed, config=config)

        inserted = 0
        with conn.cursor() as cur:
            for (idx, node), embedding in zip(to_insert, embeddings):
                metadata = dict(node.metadata)
                metadata["chunk_id"] = node.id_
                cur.execute(
                    "INSERT INTO document_chunks (content, embedding, metadata) VALUES (%s, %s, %s);",
                    (node.text, embedding, Json(metadata)),
                )
                inserted += 1
            conn.commit()

        print(f"[KORE] Stored {inserted} new chunks ({len(existing)} duplicates skipped).")
        return inserted
    finally:
        conn.close()


def similarity_search(query: str, top_k: int = 15) -> list[dict[str, Any]]:
    """Vector similarity search for the given query text.

    Returns chunks where cosine similarity >= SIMILARITY_THRESHOLD.
    """
    config = get_config()
    query_embedding = embed_text(query, config=config)

    conn = _connect()
    _register_vector(conn)

    try:
        with conn.cursor() as cur:
            # cosine distance: <=> (0 = identical, 2 = opposite)
            # similarity = 1 - distance
            cur.execute(
                """
                SELECT
                    id,
                    content,
                    metadata,
                    1 - (embedding <=> %s::vector) AS score
                FROM document_chunks
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
                """,
                (query_embedding, query_embedding, top_k),
            )
            rows = cur.fetchall()

        results = []
        for row in rows:
            chunk_id, content, metadata, score = row
            if score >= SIMILARITY_THRESHOLD:
                results.append({
                    "chunk_id": metadata.get("chunk_id") if metadata else str(chunk_id),
                    "content": content,
                    "metadata": metadata or {},
                    "score": round(score, 4),
                })

        return results
    finally:
        conn.close()


def get_chunks_by_ids(ids: list[str]) -> list[dict[str, Any]]:
    """Fetch chunks by their chunk_id stored in metadata."""
    if not ids:
        return []

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content, metadata FROM document_chunks "
                "WHERE metadata->>'chunk_id' = ANY(%s);",
                (ids,),
            )
            rows = cur.fetchall()

        return [
            {
                "chunk_id": row[1].get("chunk_id") if row[1] else None,
                "content": row[0],
                "metadata": row[1] or {},
            }
            for row in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test block
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    print("[KORE] Testing vector_store.py...")
    print(f"[KORE] DATABASE_URL: {_get_connection_url()[:30]}...")

    try:
        init_db()
    except SystemExit as e:
        print("[KORE] init_db exited with code", e.code)
        sys.exit(e.code)
    except Exception as exc:
        print(f"[KORE] init_db failed: {exc}")
        sys.exit(1)

    # Try a dummy similarity search (will be empty on fresh DB)
    try:
        results = similarity_search("test query", top_k=5)
        print(f"[KORE] Similarity search returned {len(results)} results.")
    except Exception as exc:
        print(f"[KORE] similarity_search failed: {exc}")
