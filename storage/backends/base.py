from abc import ABC, abstractmethod
from typing import Any

from llama_index.core.schema import TextNode


class VectorStoreBackend(ABC):

    @abstractmethod
    def init_db(self) -> None:
        ...

    @abstractmethod
    def store_chunks(self, nodes: list[TextNode]) -> int:
        ...

    @abstractmethod
    def similarity_search(self, query: str, top_k: int = 15) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_chunks_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_chunk_count(self) -> int:
        ...

    @abstractmethod
    def get_chunk_stats(self) -> dict[str, int]:
        ...

    @abstractmethod
    def reset(self) -> None:
        ...

    @abstractmethod
    def health_check(self) -> bool:
        ...
