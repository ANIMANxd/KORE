import json
import os
import time
from typing import Any

import psycopg2
from psycopg2.extras import Json

from llama_index.core.schema import TextNode

from providers.config import get_config
from providers.embedder import embed_text, embed_batch
from storage.backends.base import VectorStoreBackend

SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.40"))
_RETRY_ATTEMPTS = 3


class PgvectorBackend(VectorStoreBackend):

    def __init__(self) -> None:
        config = get_config()
        self._url = config.pgvector_url
        if not self._url:
            raise RuntimeError(
                "DATABASE_URL not set. Add pgvector.url to provider.config.yaml, or "
                "set DATABASE_URL in .env, e.g.:\n"
                "DATABASE_URL=postgresql://postgres:password@localhost:5432/kore"
            )

    def _connect(self) -> psycopg2.extensions.connection:
        last_error: Exception | None = None
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                conn = psycopg2.connect(self._url)
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

    def _register_vector(self, conn: psycopg2.extensions.connection) -> None:
        try:
            from pgvector.psycopg2 import register_vector
            register_vector(conn)
        except ImportError:
            from psycopg2.extensions import new_type, new_array_type, register_type
            vector_type = new_type((16384,), 'VECTOR', lambda value, cursor: value)
            register_type(vector_type, conn)

    def _get_existing_vector_dim(self, conn: psycopg2.extensions.connection) -> int | None:
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

    def init_db(self) -> None:
        config = get_config()
        expected_dim = config.embedding_dimensions

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                conn.commit()

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
                existing_dim = self._get_existing_vector_dim(conn)
                if existing_dim is not None and existing_dim != expected_dim:
                    print(
                        f"[KORE] ERROR: Embedding dimensions changed.\n"
                        f"  Table 'document_chunks' expects vector({existing_dim}),\n"
                        f"  but provider.config.yaml specifies {expected_dim}.\n"
                        f"  Run 'python run.py reset-db' to reinitialise."
                    )
                    raise SystemExit(1)
                print("[KORE] Vector store: pgvector (table already exists)")
                return

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
                cur.execute("""
                    CREATE INDEX idx_document_chunks_embedding
                    ON document_chunks USING ivfflat (embedding vector_cosine_ops);
                """)
                conn.commit()
            print(f"[KORE] Vector store: pgvector (created table with vector({expected_dim}))")
        finally:
            conn.close()

    def store_chunks(self, nodes: list[TextNode]) -> int:
        if not nodes:
            return 0

        config = get_config()
        conn = self._connect()
        self._register_vector(conn)

        try:
            texts = [node.text for node in nodes]
            chunk_ids = [node.id_ for node in nodes]

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

    def similarity_search(self, query: str, top_k: int = 15) -> list[dict[str, Any]]:
        config = get_config()
        query_embedding = embed_text(query, config=config)

        conn = self._connect()
        self._register_vector(conn)

        try:
            with conn.cursor() as cur:
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

    def get_chunks_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []

        conn = self._connect()
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

    def get_chunk_count(self) -> int:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM document_chunks;")
                return cur.fetchone()[0]
        finally:
            conn.close()

    def get_chunk_stats(self) -> dict[str, int]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT metadata->>'source_type', COUNT(*) "
                    "FROM document_chunks GROUP BY metadata->>'source_type';"
                )
                return {row[0] or "unknown": row[1] for row in cur.fetchall()}
        finally:
            conn.close()

    def reset(self) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS document_chunks;")
                conn.commit()
        finally:
            conn.close()
        self.init_db()

    def health_check(self) -> bool:
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
            conn.close()
            return True
        except Exception:
            return False
