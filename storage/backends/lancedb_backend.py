import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa

from llama_index.core.schema import TextNode

from providers.config import get_config
from providers.embedder import embed_text, embed_batch
from storage.backends.base import VectorStoreBackend

SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.40"))


class LanceDBBackend(VectorStoreBackend):

    def __init__(self) -> None:
        config = get_config()
        raw = config.lancedb_data_dir
        self._data_dir = Path(os.path.expanduser(raw)).resolve()
        self._db_path = self._data_dir / "vectors"
        self._table_name = "document_chunks"
        self._db = None
        self._table = None

    def _get_db(self):
        if self._db is None:
            import lancedb
            self._db_path.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self._db_path))
        return self._db

    def _get_table(self):
        if self._table is None:
            db = self._get_db()
            self._table = db.open_table(self._table_name)
        return self._table

    def _has_table(self) -> bool:
        db = self._get_db()
        return self._table_name in db.table_names()

    def init_db(self) -> None:
        config = get_config()
        expected_dim = config.embedding_dimensions

        self._data_dir.mkdir(parents=True, exist_ok=True)
        db = self._get_db()

        if self._has_table():
            table = db.open_table(self._table_name)
            schema = table.schema
            vector_field = schema.field("embedding")
            existing_dim = vector_field.type.list_size
            if existing_dim != expected_dim:
                print(
                    f"[KORE] ERROR: Embedding dimensions changed.\n"
                    f"  LanceDB table '{self._table_name}' has vector({existing_dim}),\n"
                    f"  but provider.config.yaml specifies {expected_dim}.\n"
                    f"  Run 'python run.py reset-db' to reinitialise."
                )
                raise SystemExit(1)
            print(f"[KORE] Vector store: LanceDB at {self._db_path}")
            return

        schema = pa.schema([
            pa.field("chunk_id", pa.string()),
            pa.field("content", pa.string()),
            pa.field("embedding", pa.list_(pa.float32(), expected_dim)),
            pa.field("metadata", pa.string()),
            pa.field("created_at", pa.string()),
        ])

        db.create_table(self._table_name, schema=schema)
        self._table = None
        print(f"[KORE] Vector store: LanceDB at {self._db_path}")

    def store_chunks(self, nodes: list[TextNode]) -> int:
        if not nodes:
            return 0

        config = get_config()
        db = self._get_db()

        texts = [node.text for node in nodes]
        chunk_ids = [node.id_ for node in nodes]

        existing_ids: set[str] = set()
        if self._has_table():
            try:
                table = db.open_table(self._table_name)
                df = table.to_pandas()
                if "chunk_id" in df.columns:
                    existing_ids = set(df["chunk_id"].tolist())
            except Exception:
                existing_ids = set()

        to_insert = [(i, node) for i, node in enumerate(nodes) if node.id_ not in existing_ids]

        if not to_insert:
            print("[KORE] All chunks already stored. Nothing new to insert.")
            return 0

        indices_to_embed = [idx for idx, _ in to_insert]
        texts_to_embed = [texts[idx] for idx in indices_to_embed]
        embeddings = embed_batch(texts_to_embed, config=config)

        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for (idx, node), embedding in zip(to_insert, embeddings):
            metadata = dict(node.metadata)
            metadata["chunk_id"] = node.id_
            rows.append({
                "chunk_id": node.id_,
                "content": node.text,
                "embedding": embedding,
                "metadata": json.dumps(metadata, ensure_ascii=False),
                "created_at": now,
            })

        if self._has_table():
            table = db.open_table(self._table_name)
            table.add(rows)
        else:
            db.create_table(self._table_name, data=rows)

        self._table = None
        inserted = len(rows)
        skipped = len(nodes) - inserted
        if skipped > 0:
            print(f"[KORE] Stored {inserted} new chunks ({skipped} duplicates skipped).")
        else:
            print(f"[KORE] Stored {inserted} new chunks.")
        return inserted

    def similarity_search(self, query: str, top_k: int = 15) -> list[dict[str, Any]]:
        config = get_config()
        query_embedding = embed_text(query, config=config)

        table = self._get_table()
        try:
            raw = table.search(query_embedding).metric("cosine").limit(top_k).to_list()
        except Exception:
            raw = table.search(query_embedding).limit(top_k).to_list()

        results = []
        for row in raw:
            distance = row.get("_distance", 1.0)
            score = round(1.0 - distance, 4)

            if score < SIMILARITY_THRESHOLD:
                continue

            meta_str = row.get("metadata", "{}")
            try:
                metadata = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
            except (json.JSONDecodeError, TypeError):
                metadata = {}

            results.append({
                "chunk_id": row.get("chunk_id", metadata.get("chunk_id", "")),
                "content": row.get("content", ""),
                "metadata": metadata,
                "score": score,
            })

        return results

    def get_chunks_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []

        table = self._get_table()
        df = table.to_pandas()

        if "chunk_id" not in df.columns:
            return []

        target_ids = set(ids)
        matching = df[df["chunk_id"].isin(target_ids)]

        result = []
        for _, row in matching.iterrows():
            meta_str = row.get("metadata", "{}")
            try:
                metadata = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            result.append({
                "chunk_id": row.get("chunk_id", metadata.get("chunk_id", None)),
                "content": row.get("content", ""),
                "metadata": metadata,
            })
        return result

    def get_chunk_count(self) -> int:
        table = self._get_table()
        return table.count_rows()

    def get_chunk_stats(self) -> dict[str, int]:
        table = self._get_table()
        df = table.to_pandas()
        if "metadata" not in df.columns:
            return {}

        stats: dict[str, int] = {}
        for _, row in df.iterrows():
            meta_str = row.get("metadata", "{}")
            try:
                metadata = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            source = metadata.get("source_type", "unknown")
            stats[source] = stats.get(source, 0) + 1
        return stats

    def reset(self) -> None:
        db = self._get_db()
        if self._has_table():
            db.drop_table(self._table_name)
        self._table = None
        self.init_db()

    def health_check(self) -> bool:
        try:
            db = self._get_db()
            _ = db.table_names()
            return True
        except Exception:
            return False
