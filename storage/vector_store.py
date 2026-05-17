"""
KORE vector store — public interface.

Backend-agnostic. All modules outside storage/ call these functions.
Backend selection is read from provider.config.yaml at runtime.
"""

from typing import Any

from llama_index.core.schema import TextNode

from storage.backends.base import VectorStoreBackend

_backend_instance: VectorStoreBackend | None = None


def _get_backend() -> VectorStoreBackend:
    global _backend_instance
    if _backend_instance is None:
        from providers.config import get_config
        from storage.backends.lancedb_backend import LanceDBBackend
        from storage.backends.pgvector_backend import PgvectorBackend

        config = get_config()
        backend_name = config.vector_store_backend

        if backend_name == "lancedb":
            _backend_instance = LanceDBBackend()
        elif backend_name == "pgvector":
            _backend_instance = PgvectorBackend()
        else:
            raise ValueError(
                f"Unknown vector store backend: {backend_name!r}. "
                f"Valid options: lancedb, pgvector"
            )

    return _backend_instance


def init_db() -> None:
    _get_backend().init_db()


def store_chunks(nodes: list[TextNode]) -> int:
    return _get_backend().store_chunks(nodes)


def similarity_search(query: str, top_k: int = 15) -> list[dict[str, Any]]:
    return _get_backend().similarity_search(query, top_k)


def get_chunks_by_ids(ids: list[str]) -> list[dict[str, Any]]:
    return _get_backend().get_chunks_by_ids(ids)


def get_chunk_count() -> int:
    return _get_backend().get_chunk_count()


def get_chunk_stats() -> dict[str, int]:
    return _get_backend().get_chunk_stats()


def reset_db() -> None:
    _get_backend().reset()


def health_check() -> bool:
    return _get_backend().health_check()
