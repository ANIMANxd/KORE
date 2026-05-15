from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    provider_type: Literal["cloud", "local"]
    requires_api_key: bool
    requires_base_url: bool
    default_base_url: str | None
    litellm_prefix: str
    embedding_supported: bool
    model_name_hint: str


PROVIDER_REGISTRY: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        provider_type="cloud",
        requires_api_key=True,
        requires_base_url=False,
        default_base_url=None,
        litellm_prefix="",
        embedding_supported=True,
        model_name_hint="Check platform.openai.com/docs/models for the latest available models",
    ),
    "anthropic": ProviderSpec(
        name="anthropic",
        provider_type="cloud",
        requires_api_key=True,
        requires_base_url=False,
        default_base_url=None,
        litellm_prefix="anthropic/",
        embedding_supported=False,
        model_name_hint="Check docs.anthropic.com for the latest available models",
    ),
    "gemini": ProviderSpec(
        name="gemini",
        provider_type="cloud",
        requires_api_key=True,
        requires_base_url=False,
        default_base_url=None,
        litellm_prefix="gemini/",
        embedding_supported=True,
        model_name_hint="Check ai.google.dev/models for the latest available models",
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        provider_type="cloud",
        requires_api_key=True,
        requires_base_url=False,
        default_base_url=None,
        litellm_prefix="",
        embedding_supported=True,
        model_name_hint="Check platform.deepseek.com for the latest available models",
    ),
    "mistral": ProviderSpec(
        name="mistral",
        provider_type="cloud",
        requires_api_key=True,
        requires_base_url=False,
        default_base_url=None,
        litellm_prefix="",
        embedding_supported=False,
        model_name_hint="Check docs.mistral.ai for the latest available models",
    ),
    "cohere": ProviderSpec(
        name="cohere",
        provider_type="cloud",
        requires_api_key=True,
        requires_base_url=False,
        default_base_url=None,
        litellm_prefix="",
        embedding_supported=True,
        model_name_hint="Check docs.cohere.com for the latest available models",
    ),
    "azure": ProviderSpec(
        name="azure",
        provider_type="cloud",
        requires_api_key=True,
        requires_base_url=True,
        default_base_url=None,
        litellm_prefix="azure/",
        embedding_supported=True,
        model_name_hint="Check your Azure OpenAI deployment URL and model deployment name",
    ),
    "bedrock": ProviderSpec(
        name="bedrock",
        provider_type="cloud",
        requires_api_key=True,
        requires_base_url=False,
        default_base_url=None,
        litellm_prefix="bedrock/",
        embedding_supported=False,
        model_name_hint="Check AWS Bedrock model IDs in your AWS console",
    ),
    "ollama": ProviderSpec(
        name="ollama",
        provider_type="local",
        requires_api_key=False,
        requires_base_url=True,
        default_base_url="http://localhost:11434",
        litellm_prefix="ollama/",
        embedding_supported=True,
        model_name_hint="Run `ollama list` to see installed models, or check ollama.com/library",
    ),
    "lmstudio": ProviderSpec(
        name="lmstudio",
        provider_type="local",
        requires_api_key=False,
        requires_base_url=True,
        default_base_url="http://localhost:1234/v1",
        litellm_prefix="openai/",
        embedding_supported=True,
        model_name_hint="Check the LM Studio UI → Developer → Local Server for loaded model names",
    ),
    "localai": ProviderSpec(
        name="localai",
        provider_type="local",
        requires_api_key=False,
        requires_base_url=True,
        default_base_url=None,
        litellm_prefix="",
        embedding_supported=True,
        model_name_hint="Check your LocalAI instance for available models",
    ),
    "custom": ProviderSpec(
        name="custom",
        provider_type="local",
        requires_api_key=False,
        requires_base_url=True,
        default_base_url=None,
        litellm_prefix="",
        embedding_supported=True,
        model_name_hint="Consult your provider's documentation for the exact model name string",
    ),
}


def get_provider(name: str) -> ProviderSpec:
    if name not in PROVIDER_REGISTRY:
        raise ValueError(f"Unknown provider: {name!r}. Supported: {', '.join(PROVIDER_REGISTRY)}")
    return PROVIDER_REGISTRY[name]


def list_providers(provider_type: str | None = None) -> list[str]:
    if provider_type is None:
        return sorted(PROVIDER_REGISTRY.keys())
    return sorted(
        name for name, spec in PROVIDER_REGISTRY.items() if spec.provider_type == provider_type
    )


def get_litellm_model_string(provider: str, model: str) -> str:
    spec = get_provider(provider)
    return f"{spec.litellm_prefix}{model}"
