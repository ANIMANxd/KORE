import os
import time
import urllib.request
import urllib.error
import json as _json
from typing import Any

from providers.config import ProviderConfig, get_config, get_litellm_embedding_string

try:
    import litellm
except ImportError:
    litellm = None  # type: ignore

_BATCH_SIZE = 100
_RETRY_ATTEMPTS = 3


def _call_litellm_embedding_with_retry(
    model: str,
    input_texts: list[str],
    api_key: str | None,
    api_base: str | None,
) -> list[list[float]]:
    if litellm is None:
        raise RuntimeError("litellm is not installed. Run: pip install litellm")

    kwargs: dict[str, Any] = {
        "model": model,
        "input": input_texts,
    }
    # LiteLLM requires an api_key even for local providers
    kwargs["api_key"] = api_key or "not-needed"
    if api_base is not None:
        kwargs["api_base"] = api_base

    last_error: Exception | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            response = litellm.embedding(**kwargs)
            return [item["embedding"] for item in response["data"]]
        except Exception as exc:
            last_error = exc
            error_str = str(exc).lower()
            is_rate_limit = any(
                phrase in error_str
                for phrase in ("rate limit", "ratelimit", "too many requests", "429")
            )
            if is_rate_limit and attempt < _RETRY_ATTEMPTS:
                sleep_seconds = 2 ** attempt
                print(f"[KORE] Embedding rate limit hit (attempt {attempt}/{_RETRY_ATTEMPTS}). "
                      f"Retrying in {sleep_seconds}s...")
                time.sleep(sleep_seconds)
                continue
            raise

    raise last_error or RuntimeError("Embedding call failed")


def _embed_ollama(text: str, config: ProviderConfig) -> list[float]:
    base_url = config.embedding_base_url or "http://localhost:11434"
    url = f"{base_url.rstrip('/')}/api/embeddings"
    payload = _json.dumps({"model": config.embedding_model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_error: Exception | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                data = _json.loads(response.read().decode("utf-8"))
                return data["embedding"]
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and attempt < _RETRY_ATTEMPTS:
                sleep_seconds = 2 ** attempt
                print(f"[KORE] Ollama embedding rate limit (attempt {attempt}/{_RETRY_ATTEMPTS}). "
                      f"Retrying in {sleep_seconds}s...")
                time.sleep(sleep_seconds)
                continue
            raise
        except Exception as exc:
            last_error = exc
            if attempt < _RETRY_ATTEMPTS:
                sleep_seconds = 2 ** attempt
                print(f"[KORE] Ollama embedding error (attempt {attempt}/{_RETRY_ATTEMPTS}). "
                      f"Retrying in {sleep_seconds}s...")
                time.sleep(sleep_seconds)
                continue
            raise

    raise last_error or RuntimeError("Ollama embedding call failed")


def _embed_ollama_batch(texts: list[str], config: ProviderConfig) -> list[list[float]]:
    # Ollama does not natively support batch embeddings in a single call,
    # so we loop. We still chunk to be polite to the server.
    results: list[list[float]] = []
    total = len(texts)
    for i, text in enumerate(texts):
        results.append(_embed_ollama(text, config))
        if total > _BATCH_SIZE and (i + 1) % _BATCH_SIZE == 0:
            print(f"[KORE] Ollama embedding progress: {i + 1}/{total}")
    return results


def embed_text(text: str, config: ProviderConfig | None = None) -> list[float]:
    if config is None:
        config = get_config()

    if config.embedding_provider == "ollama":
        return _embed_ollama(text, config)

    model = get_litellm_embedding_string(config)
    embeddings = _call_litellm_embedding_with_retry(
        model=model,
        input_texts=[text],
        api_key=config.embedding_api_key,
        api_base=config.embedding_base_url,
    )
    return embeddings[0]


def embed_batch(texts: list[str], config: ProviderConfig | None = None) -> list[list[float]]:
    if config is None:
        config = get_config()

    if not texts:
        return []

    if config.embedding_provider == "ollama":
        return _embed_ollama_batch(texts, config)

    model = get_litellm_embedding_string(config)
    total = len(texts)
    results: list[list[float]] = []
    batch_count = (total + _BATCH_SIZE - 1) // _BATCH_SIZE
    show_progress = batch_count > 3

    for i in range(0, total, _BATCH_SIZE):
        batch = texts[i : i + _BATCH_SIZE]
        batch_embeddings = _call_litellm_embedding_with_retry(
            model=model,
            input_texts=batch,
            api_key=config.embedding_api_key,
            api_base=config.embedding_base_url,
        )
        results.extend(batch_embeddings)

        if show_progress:
            current_batch = i // _BATCH_SIZE + 1
            print(f"[KORE] Embedding batch {current_batch}/{batch_count} complete")

    return results


def get_embedding_dimensions(config: ProviderConfig | None = None, validate: bool = False) -> int:
    if config is None:
        config = get_config()

    declared = config.embedding_dimensions

    if validate:
        try:
            test_embedding = embed_text("test", config=config)
            actual = len(test_embedding)
            if actual != declared:
                print(
                    f"[KORE] WARNING: Embedding dimension mismatch. "
                    f"Config says {declared}, but actual embedding has {actual} dimensions. "
                    f"Update embedding.dimensions in provider.config.yaml to match your model."
                )
            return actual
        except Exception as exc:
            print(f"[KORE] WARNING: Could not validate embedding dimensions: {exc}")

    return declared
