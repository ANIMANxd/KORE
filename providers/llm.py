import json
import os
import re
import time
from typing import Any

from providers.config import ProviderConfig, get_config, get_litellm_model_string

# Optional lenient JSON parser
try:
    import json5
except ImportError:
    json5 = None  # type: ignore

try:
    import litellm
except ImportError:
    litellm = None  # type: ignore


MAX_JSON_RETRIES = int(os.getenv("MAX_JSON_RETRIES", "3"))
_REQUEST_TIMEOUT = 60
_RATE_LIMIT_RETRIES = 3

# Providers known to support response_format={"type": "json_object"}
_JSON_FORMAT_SUPPORTED = {"openai", "anthropic", "gemini"}


class JSONRepairFailed(Exception):
    """Raised when all JSON repair attempts have been exhausted."""

    def __init__(self, message: str, raw_output: str, attempts: int):
        super().__init__(message)
        self.raw_output = raw_output
        self.attempts = attempts


def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` fences if present."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _build_messages(
    messages: list[dict[str, str]],
    system_prompt: str,
    expect_json: bool,
) -> list[dict[str, str]]:
    final_system = system_prompt.strip()
    if expect_json:
        final_system += (
            " Respond in valid JSON only. No preamble. No explanation. "
            "No markdown fences. No trailing commas."
        )
    return [{"role": "system", "content": final_system}] + list(messages)


def _call_litellm_with_retry(
    model: str,
    messages: list[dict[str, str]],
    api_key: str | None,
    api_base: str | None,
    temperature: float,
    max_tokens: int,
    use_json_format: bool,
) -> str:
    if litellm is None:
        raise RuntimeError("litellm is not installed. Run: pip install litellm")

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": _REQUEST_TIMEOUT,
    }
    kwargs["api_key"] = api_key or "lm-studio"
    if api_base is not None:
        kwargs["api_base"] = api_base
    if use_json_format:
        kwargs["response_format"] = {"type": "json_object"}

    last_error: Exception | None = None
    for attempt in range(1, _RATE_LIMIT_RETRIES + 1):
        try:
            response = litellm.completion(**kwargs)
            content = response["choices"][0]["message"]["content"]
            if content is None:
                return ""
            return content
        except Exception as exc:
            last_error = exc
            error_str = str(exc).lower()
            is_rate_limit = any(
                phrase in error_str
                for phrase in ("rate limit", "ratelimit", "too many requests", "429")
            )
            if is_rate_limit and attempt < _RATE_LIMIT_RETRIES:
                sleep_seconds = 2 ** attempt
                print(f"[KORE] Rate limit hit (attempt {attempt}/{_RATE_LIMIT_RETRIES}). "
                      f"Retrying in {sleep_seconds}s...")
                time.sleep(sleep_seconds)
                continue
            raise

    # Should never reach here because the loop raises on the last attempt,
    # but satisfy the type checker.
    raise last_error or RuntimeError("LLM call failed")


def complete(
    messages: list[dict[str, str]],
    system_prompt: str,
    config: ProviderConfig | None = None,
    expect_json: bool = True,
    max_tokens: int = 2000,
) -> str:
    if config is None:
        config = get_config()

    model = get_litellm_model_string(config)
    full_messages = _build_messages(messages, system_prompt, expect_json)
    use_json_format = expect_json and config.llm_provider in _JSON_FORMAT_SUPPORTED

    return _call_litellm_with_retry(
        model=model,
        messages=full_messages,
        api_key=config.llm_api_key,
        api_base=config.llm_base_url,
        temperature=config.llm_temperature,
        max_tokens=max_tokens,
        use_json_format=use_json_format,
    )


def _try_parse_json(raw: str) -> dict:
    cleaned = _strip_markdown_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        if json5 is not None:
            try:
                return json5.loads(cleaned)  # type: ignore
            except Exception:
                pass
        raise e


def _validate_keys(parsed: dict, expected_schema: dict) -> dict:
    required = set(expected_schema.keys())
    missing = required - set(parsed.keys())
    if missing:
        raise KeyError(f"Missing required keys: {', '.join(sorted(missing))}")
    return parsed


def complete_json(
    messages: list[dict[str, str]],
    system_prompt: str,
    expected_schema: dict,
    config: ProviderConfig | None = None,
    max_retries: int | None = None,
) -> tuple[dict, int]:
    """Call LLM with JSON output, automatically repairing parse failures.

    Returns
    -------
    tuple[dict, int]
        The parsed JSON dict and the number of repair attempts that were needed.
    """
    if max_retries is None:
        max_retries = MAX_JSON_RETRIES

    if config is None:
        config = get_config()

    raw_output = ""
    attempts = 0
    conversation = list(messages)

    for attempt in range(max_retries + 1):
        raw_output = complete(
            messages=conversation,
            system_prompt=system_prompt,
            config=config,
            expect_json=True,
        )
        attempts = attempt

        try:
            parsed = _try_parse_json(raw_output)
            validated = _validate_keys(parsed, expected_schema)
            if attempt > 0:
                print(f"[KORE] JSON repair succeeded after {attempt} attempt(s).")
            return validated, attempts
        except Exception as parse_error:
            if attempt >= max_retries:
                break

            repair_message = (
                f"Your previous response failed JSON parsing with this error: {parse_error}\n"
                f"The broken output was: {raw_output}\n"
                f"Fix it and return valid JSON only. Required fields: {', '.join(expected_schema.keys())}"
            )
            conversation.append({"role": "assistant", "content": raw_output})
            conversation.append({"role": "user", "content": repair_message})
            print(f"[KORE] JSON parse failed (attempt {attempt + 1}/{max_retries + 1}). Sending repair prompt...")

    raise JSONRepairFailed(
        message=f"JSON repair failed after {max_retries} attempt(s).",
        raw_output=raw_output,
        attempts=attempts,
    )


def test_connection(config: ProviderConfig | None = None) -> bool:
    if config is None:
        config = get_config()

    try:
        response = complete(
            messages=[{"role": "user", "content": "Reply with the word OK and nothing else."}],
            system_prompt="You are a helpful assistant.",
            config=config,
            expect_json=False,
            max_tokens=10,
        )
        ok = "ok" in response.strip().lower()
        if ok:
            print(f"[KORE] Connection OK — {config.llm_provider} / {config.llm_model}")
        else:
            print(f"[KORE] Connection test returned unexpected response: {response!r}")
        return ok
    except Exception as exc:
        print(f"[KORE] Connection FAILED — {config.llm_provider} / {config.llm_model}\n  Error: {exc}")
        return False
