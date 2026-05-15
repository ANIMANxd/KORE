from dataclasses import dataclass
from typing import Literal
from pathlib import Path

import yaml

from providers.registry import get_provider, get_litellm_model_string as _registry_litellm_string


@dataclass(frozen=True)
class ProviderConfig:
    llm_provider: str
    llm_model: str
    llm_api_key: str | None
    llm_base_url: str | None
    llm_temperature: float
    embedding_provider: str
    embedding_model: str
    embedding_api_key: str | None
    embedding_base_url: str | None
    embedding_dimensions: int
    deployment_mode: Literal["cloud", "local", "hybrid"]


def _validate_and_resolve(
    provider_name: str,
    api_key: str | None,
    base_url: str | None,
    context: str,
) -> tuple[str | None, str | None]:
    spec = get_provider(provider_name)

    if spec.requires_api_key and not api_key:
        raise ValueError(
            f"{context} provider '{provider_name}' requires an API key, "
            f"but none was provided in the config file."
        )

    if spec.requires_base_url and not base_url:
        if spec.default_base_url:
            base_url = spec.default_base_url
        else:
            raise ValueError(
                f"{context} provider '{provider_name}' requires a base_url, "
                f"but none was provided and no default is available."
            )

    return api_key, base_url


def load_config(path: str = "provider.config.yaml") -> ProviderConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Provider config file not found: {config_path.resolve()}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError(f"Provider config file is empty: {config_path.resolve()}")

    llm_raw = raw.get("llm", {})
    embedding_raw = raw.get("embedding", {})

    llm_provider = llm_raw.get("provider", "")
    llm_model = llm_raw.get("model", "")
    llm_api_key = llm_raw.get("api_key") or None
    llm_base_url = llm_raw.get("base_url") or None
    llm_temperature = float(llm_raw.get("temperature", 0.1))

    embedding_provider = embedding_raw.get("provider", "")
    embedding_model = embedding_raw.get("model", "")
    embedding_api_key = embedding_raw.get("api_key") or None
    embedding_base_url = embedding_raw.get("base_url") or None
    embedding_dimensions = int(embedding_raw.get("dimensions", 0))

    deployment_mode = raw.get("deployment_mode", "local")

    if not llm_provider:
        raise ValueError("Missing 'llm.provider' in provider config.")
    if not llm_model:
        raise ValueError("Missing 'llm.model' in provider config.")
    if not embedding_provider:
        raise ValueError("Missing 'embedding.provider' in provider config.")
    if not embedding_model:
        raise ValueError("Missing 'embedding.model' in provider config.")
    if embedding_dimensions <= 0:
        raise ValueError("Missing or invalid 'embedding.dimensions' in provider config.")

    llm_api_key, llm_base_url = _validate_and_resolve(
        llm_provider, llm_api_key, llm_base_url, context="LLM"
    )
    embedding_api_key, embedding_base_url = _validate_and_resolve(
        embedding_provider, embedding_api_key, embedding_base_url, context="Embedding"
    )

    config = ProviderConfig(
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_temperature=llm_temperature,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_api_key=embedding_api_key,
        embedding_base_url=embedding_base_url,
        embedding_dimensions=embedding_dimensions,
        deployment_mode=deployment_mode,
    )

    print(f"[KORE] LLM active: {config.llm_provider} / {config.llm_model}")
    print(f"[KORE] Embedding active: {config.embedding_provider} / {config.embedding_model}")

    return config


def get_litellm_model_string(config: ProviderConfig) -> str:
    return _registry_litellm_string(config.llm_provider, config.llm_model)


def get_litellm_embedding_string(config: ProviderConfig) -> str:
    return _registry_litellm_string(config.embedding_provider, config.embedding_model)


_config_instance: ProviderConfig | None = None


def get_config(path: str = "provider.config.yaml") -> ProviderConfig:
    global _config_instance
    if _config_instance is None:
        _config_instance = load_config(path)
    return _config_instance
