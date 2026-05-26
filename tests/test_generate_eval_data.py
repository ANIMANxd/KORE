"""Tests for scripts/generate_eval_data.py — everything mocked, zero network I/O."""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import generate_eval_data as gen


# ---------------------------------------------------------------------------
# LocalLLMClient
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


@pytest.fixture
def mock_session():
    """Return a mock requests.Session that records all POST calls."""
    session = MagicMock()
    session.headers = {}
    return session


def test_local_llm_client_chat_success(mock_session):
    fake = FakeResponse(
        {
            "choices": [
                {"message": {"content": "hello world"}}
            ]
        }
    )
    mock_session.post.return_value = fake

    client = gen.LocalLLMClient("http://localhost:8080/v1")
    # Patch the internal session
    client.session = mock_session

    text = client.chat([{"role": "user", "content": "hi"}])
    assert text == "hello world"
    mock_session.post.assert_called_once()
    url = mock_session.post.call_args[0][0]
    assert "/chat/completions" in url


def test_local_llm_client_chat_empty_choices(mock_session):
    fake = FakeResponse({"choices": []})
    mock_session.post.return_value = fake

    client = gen.LocalLLMClient("http://localhost:8080/v1")
    client.session = mock_session

    text = client.chat([{"role": "user", "content": "hi"}])
    assert text == ""


def test_local_llm_client_chat_http_error(mock_session):
    fake = FakeResponse({}, status_code=500)
    mock_session.post.return_value = fake

    client = gen.LocalLLMClient("http://localhost:8080/v1")
    client.session = mock_session

    with pytest.raises(Exception, match="HTTP 500"):
        client.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# Phase 1 — Atomic single-rule generation
# ---------------------------------------------------------------------------


def test_generate_single_rule_success(mock_session):
    rule = {
        "rule_text": "All production deploys require a second pair of eyes.",
        "rule_category": "engineering",
        "confidence": 0.95,
    }
    fake = FakeResponse({"choices": [{"message": {"content": json.dumps(rule)}}]})
    mock_session.post.return_value = fake

    client = gen.LocalLLMClient("http://localhost:8080/v1")
    client.session = mock_session

    result = gen.generate_single_rule(client, category="engineering", rule_id=7)
    assert isinstance(result, dict)
    assert result["rule_id"] == 7
    assert result["rule_category"] == "engineering"
    assert "created_at" in result
    assert result["rule_text"] == rule["rule_text"]


def test_generate_single_rule_wrapped_in_fences(mock_session):
    rule = {"rule_text": "R1", "rule_category": "ops", "confidence": 0.9}
    raw = "```json\n" + json.dumps(rule) + "\n```"
    fake = FakeResponse({"choices": [{"message": {"content": raw}}]})
    mock_session.post.return_value = fake

    client = gen.LocalLLMClient("http://localhost:8080/v1")
    client.session = mock_session

    result = gen.generate_single_rule(client, category="ops", rule_id=1)
    assert result["rule_text"] == "R1"
    assert result["rule_category"] == "ops"
    assert result["rule_id"] == 1


def test_generate_single_rule_bad_json_raises(mock_session):
    fake = FakeResponse({"choices": [{"message": {"content": json.dumps("not a dict")}}]})
    mock_session.post.return_value = fake

    client = gen.LocalLLMClient("http://localhost:8080/v1")
    client.session = mock_session

    with pytest.raises(ValueError, match="Expected JSON object"):
        gen.generate_single_rule(client, category="hr", rule_id=1)


# ---------------------------------------------------------------------------
# Phase 2 — Platform noise generation (per connector)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("connector", gen.CONNECTORS)
def test_generate_platform_noise_all_connectors(mock_session, connector):
    fake = FakeResponse(
        {"choices": [{"message": {"content": f"[{connector}] noise text here"}}]}
    )
    mock_session.post.return_value = fake

    client = gen.LocalLLMClient("http://localhost:8080/v1")
    client.session = mock_session

    rule = {"rule_text": "Test rule."}
    text = gen.generate_platform_noise(client, rule, connector)
    assert f"[{connector}]" in text


def test_generate_platform_noise_connection_retry_then_success(mock_session):
    """Simulate connection failure on first call, success on retry."""
    fake_ok = FakeResponse(
        {"choices": [{"message": {"content": "success after retry"}}]}
    )
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("boom")
        return fake_ok

    mock_session.post.side_effect = side_effect

    client = gen.LocalLLMClient("http://localhost:8080/v1")
    client.session = mock_session

    rule = {"rule_text": "R"}
    text = gen.generate_platform_noise(client, rule, "slack")
    assert text == "success after retry"
    assert call_count == 2


def test_generate_platform_noise_connection_retry_exhausted(mock_session):
    """Simulate connection failure on every attempt."""
    mock_session.post.side_effect = ConnectionError("boom")

    client = gen.LocalLLMClient("http://localhost:8080/v1")
    client.session = mock_session

    rule = {"rule_text": "R"}
    with pytest.raises(ConnectionError, match="boom"):
        gen.generate_platform_noise(client, rule, "slack")
    assert mock_session.post.call_count == 3  # max_retries=3


# ---------------------------------------------------------------------------
# Phase 3 — Metadata injection
# ---------------------------------------------------------------------------


def _minimal_rule() -> dict:
    return {"rule_id": 42, "rule_text": "T", "rule_category": "ops", "confidence": 0.9}


@pytest.mark.parametrize("connector", gen.CONNECTORS)
def test_inject_metadata_has_source_type(connector):
    meta = gen.inject_metadata(_minimal_rule(), connector, "some text")
    assert meta["source_type"] == connector
    assert meta["rule_id"] == 42


def test_inject_metadata_slack_fields():
    meta = gen.inject_metadata(_minimal_rule(), "slack", "text")
    assert meta["channel"].startswith("#")
    assert isinstance(meta["timestamp"], str)
    assert "user" in meta


def test_inject_metadata_github_fields():
    meta = gen.inject_metadata(_minimal_rule(), "github", "text")
    assert "/" in meta["repo"]
    assert isinstance(meta["pr_number"], int)
    assert meta["url"].startswith("https://github.com")


def test_inject_metadata_notion_fields():
    meta = gen.inject_metadata(_minimal_rule(), "notion", "text")
    assert len(meta["page_id"]) == 8  # hex
    assert "Policy" in meta["title"]


def test_inject_metadata_jira_fields():
    meta = gen.inject_metadata(_minimal_rule(), "jira", "text")
    assert meta["issue_key"].startswith("ACME-")
    assert meta["issue_type"] in ("Task", "Story", "Bug")


def test_inject_metadata_teams_fields():
    meta = gen.inject_metadata(_minimal_rule(), "teams", "text")
    assert "team_name" in meta
    assert "channel_name" in meta
    assert "conversation_id" in meta


def test_inject_metadata_confluence_fields():
    meta = gen.inject_metadata(_minimal_rule(), "confluence", "text")
    assert meta["space_key"] in ("ENG", "OPS", "LEGAL", "HR")
    assert "page_id" in meta


def test_inject_metadata_linear_fields():
    meta = gen.inject_metadata(_minimal_rule(), "linear", "text")
    assert meta["team_key"] in ("ENG", "OPS", "PROD", "DES")
    assert meta["state"] in ("backlog", "in_progress", "done", "canceled")
    assert 0 <= meta["priority"] <= 4


def test_inject_metadata_zendesk_macro():
    # Force macro by controlling random — tricky, so we just check keys exist
    meta = gen.inject_metadata(_minimal_rule(), "zendesk", "text")
    assert meta["doc_type"] in ("macro", "ticket", "article")
    assert "title" in meta
    assert "status" in meta
    assert "last_modified" in meta
    if meta["doc_type"] == "macro":
        assert "macro_id" in meta
        assert "usage_7d" in meta
    elif meta["doc_type"] == "ticket":
        assert "ticket_id" in meta
        assert "assignee_id" in meta
        assert isinstance(meta["comments"], list)
    elif meta["doc_type"] == "article":
        assert "article_id" in meta


def test_inject_metadata_google_drive_fields():
    meta = gen.inject_metadata(_minimal_rule(), "google_drive", "text")
    assert meta["file_name"].endswith(".md")
    assert meta["mime_type"] == "text/markdown"


def test_inject_metadata_documents_fields():
    meta = gen.inject_metadata(_minimal_rule(), "documents", "text")
    assert meta["file_type"] in ("md", "txt", "pdf")
    assert meta["file_path"].startswith("/data/synthetic/documents/")
    assert meta["likely_explicit_policy"] is True
    if meta["file_type"] == "pdf":
        assert "page_count" in meta


# ---------------------------------------------------------------------------
# StreamingJsonlWriter
# ---------------------------------------------------------------------------


def test_streaming_writer_creates_file(tmp_path: Path):
    path = tmp_path / "out.jsonl"
    with gen.StreamingJsonlWriter(path) as writer:
        writer.write({"a": 1})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"a": 1}


def test_streaming_writer_multiple_writes(tmp_path: Path):
    path = tmp_path / "out.jsonl"
    with gen.StreamingJsonlWriter(path) as writer:
        writer.write({"a": 1})
        writer.write({"b": 2})
        writer.write({"c": 3})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[2]) == {"c": 3}


def test_streaming_writer_throttle(tmp_path: Path):
    path = tmp_path / "out.jsonl"
    t0 = time.monotonic()
    with gen.StreamingJsonlWriter(path, throttle_ms=50) as writer:
        writer.write({"a": 1})
    t1 = time.monotonic()
    assert (t1 - t0) >= 0.04  # allow scheduling jitter


# ---------------------------------------------------------------------------
# retry_on_connection_error decorator
# ---------------------------------------------------------------------------


def test_retry_decorator_success_first_try():
    calls = []

    @gen.retry_on_connection_error(max_retries=3, base_delay=0.01)
    def ok():
        calls.append(1)
        return "ok"

    assert ok() == "ok"
    assert calls == [1]


def test_retry_decorator_retries_then_raises():
    calls = []

    @gen.retry_on_connection_error(max_retries=2, base_delay=0.01)
    def fail():
        calls.append(1)
        raise ConnectionError("x")

    with pytest.raises(ConnectionError, match="x"):
        fail()
    assert calls == [1, 1]


def test_retry_decorator_non_connection_error_not_retried():
    calls = []

    @gen.retry_on_connection_error(max_retries=3, base_delay=0.01)
    def boom():
        calls.append(1)
        raise ValueError("x")

    with pytest.raises(ValueError, match="x"):
        boom()
    assert calls == [1]


# ---------------------------------------------------------------------------
# run() — full pipeline (fully mocked client)
# ---------------------------------------------------------------------------


def test_run_full_pipeline(tmp_path: Path):
    """End-to-end test with a fake client so no network calls happen."""
    import generate_eval_data as gen_mod

    # Build a fake client whose .chat() returns deterministic canned responses.
    class FakeClient:
        def __init__(self):
            self._call_idx = 0

        def chat(self, messages, temperature=0.7, max_tokens=2048):
            self._call_idx += 1
            content = messages[0]["content"]
            # Phase 1: single rule generation (first 4 calls = 2 iter * 2 rules)
            if "Generate ONE distinct" in content:
                # Extract category from prompt text
                category = "engineering"
                for cat in gen_mod.RULE_CATEGORIES:
                    if f"category: {cat}" in content:
                        category = cat
                        break
                rule = {
                    "rule_text": f"Rule {self._call_idx}",
                    "rule_category": category,
                    "confidence": 0.95,
                }
                return json.dumps(rule)
            # Phase 2: platform noise
            return f"Synthetic noise for call {self._call_idx}"

    fake_client = FakeClient()

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with patch.object(gen_mod, "LocalLLMClient", return_value=fake_client):
            gen_mod.run(
                target_size_gb=0.0001,  # tiny target so it stops after first few variants
                iterations=2,
                api_base="http://test",
                api_key=None,
                model="test-model",
                rules_per_iter=2,
                throttle_ms=0,
                seed=42,
            )
    finally:
        os.chdir(old_cwd)

    # Verify ground truth file exists and has 4 rules (2 iter * 2 rules)
    gt_path = tmp_path / "data" / "synthetic" / "ground_truth.json"
    assert gt_path.exists()
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    assert len(gt) == 4
    assert all("created_at" in r for r in gt)
    # Categories should cycle through RULE_CATEGORIES
    assert gt[0]["rule_category"] == gen_mod.RULE_CATEGORIES[0]
    assert gt[1]["rule_category"] == gen_mod.RULE_CATEGORIES[1]

    # Verify at least one connector got a jsonl file.
    slack_path = tmp_path / "data" / "synthetic" / "slack" / "slack_synthetic.jsonl"
    assert slack_path.exists()
    lines = slack_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) > 0
    first = json.loads(lines[0])
    assert "text" in first
    assert "metadata" in first
    assert first["metadata"]["source_type"] == "slack"
    # No fallback should have been applied in this happy-path test
    assert "synthetic_fallback_applied" not in first["metadata"]


def test_run_fallback_records_marked(tmp_path: Path):
    """Fallback records must carry synthetic_fallback_applied=True."""
    import generate_eval_data as gen_mod

    class FakeClient:
        def chat(self, messages, temperature=0.7, max_tokens=2048):
            content = messages[0]["content"]
            if "Generate ONE distinct" in content:
                return json.dumps({
                    "rule_text": "Test rule",
                    "rule_category": "engineering",
                    "confidence": 0.95,
                })
            return "noise"

    fake_client = FakeClient()

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with patch.object(gen_mod, "LocalLLMClient", return_value=fake_client):
            with patch.object(
                gen_mod, "generate_platform_noise", side_effect=Exception("boom")
            ):
                gen_mod.run(
                    target_size_gb=10.0,
                    iterations=1,
                    api_base="http://test",
                    api_key=None,
                    model="test-model",
                    rules_per_iter=1,
                    throttle_ms=0,
                    seed=42,
                )
    finally:
        os.chdir(old_cwd)

    # Every connector file should have exactly one fallback record
    for conn in gen_mod.CONNECTORS:
        p = tmp_path / "data" / "synthetic" / conn / f"{conn}_synthetic.jsonl"
        assert p.exists(), f"Missing file for {conn}"
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1, f"Expected 1 record for {conn}, got {len(lines)}"
        rec = json.loads(lines[0])
        assert rec["metadata"].get("synthetic_fallback_applied") is True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_defaults():
    with patch.object(gen, "run") as mock_run:
        gen.main(["--target-size-gb", "0.5", "--iterations", "3"])
        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs["target_size_gb"] == 0.5
        assert kwargs["iterations"] == 3
        assert kwargs["api_base"] == "http://127.0.0.1:8080/v1"


def test_cli_env_overrides(monkeypatch):
    monkeypatch.setenv("KORE_SYNTH_API_BASE", "http://env:9000/v1")
    monkeypatch.setenv("KORE_SYNTH_MODEL", "custom-model")
    with patch.object(gen, "run") as mock_run:
        gen.main([])
        kwargs = mock_run.call_args.kwargs
        assert kwargs["api_base"] == "http://env:9000/v1"
        assert kwargs["model"] == "custom-model"


def test_cli_fatal_error_prints_and_returns_1():
    with patch.object(gen, "run", side_effect=RuntimeError("boom")):
        rc = gen.main(["--iterations", "1"])
        assert rc == 1


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def test_random_timestamp_format():
    ts = gen._random_timestamp(days_back=1)
    assert "T" in ts  # ISO-8601 indicator


def test_faker_word_length():
    assert len(gen._faker_word(10)) == 10
    assert gen._faker_word(5).islower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
