import pytest
import yaml
from pydantic import ValidationError

from output.schema import BusinessRule, SourceRef, ExtractionMeta, RulesFile


class TestBusinessRuleValidation:
    """Pydantic v2 schema validations for BusinessRule."""

    def _valid_meta(self):
        return ExtractionMeta(provider="test", model="test-model", deployment_mode="local")

    def _valid_source(self):
        return SourceRef(chunk_id="c1", source_type="slack")

    def test_confidence_too_low_for_verified(self):
        """verified status with confidence < 0.5 → ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            BusinessRule(
                rule_text="Refunds processed within 48 hours for all customers.",
                rule_category="other",
                confidence=0.3,
                verification_status="verified",
                source_refs=[self._valid_source()],
                extraction_meta=self._valid_meta(),
            )
        assert "confidence" in str(exc_info.value).lower() or "verified" in str(exc_info.value).lower()

    def test_rule_text_too_short(self):
        """rule_text under 10 chars → ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            BusinessRule(
                rule_text="Too short",
                rule_category="other",
                confidence=0.9,
                verification_status="verified",
                source_refs=[self._valid_source()],
                extraction_meta=self._valid_meta(),
            )
        assert "10" in str(exc_info.value)

    def test_verified_with_no_sources(self):
        """verified rule with empty source_refs → ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            BusinessRule(
                rule_text="Refunds processed within 48 hours for all customers.",
                rule_category="other",
                confidence=0.9,
                verification_status="verified",
                source_refs=[],
                extraction_meta=self._valid_meta(),
            )
        assert "source reference" in str(exc_info.value).lower()

    def test_confidence_out_of_range(self):
        """confidence > 1.0 → ValidationError."""
        with pytest.raises(ValidationError):
            BusinessRule(
                rule_text="Refunds processed within 48 hours for all customers.",
                rule_category="other",
                confidence=1.5,
                verification_status="verified",
                source_refs=[self._valid_source()],
                extraction_meta=self._valid_meta(),
            )

    def test_valid_rule_passes(self):
        """A fully valid rule should instantiate without errors."""
        rule = BusinessRule(
            rule_text="Refunds for annual plans processed within 48 hours.",
            rule_category="other",
            confidence=0.95,
            verification_status="verified",
            source_refs=[
                SourceRef(
                    chunk_id="slack_123",
                    source_type="slack",
                    channel="#support",
                    timestamp="2024-01-01T10:00:00Z",
                    excerpt="We process refunds within 48h",
                )
            ],
            extraction_meta=ExtractionMeta(
                provider="ollama",
                model="llama3",
                deployment_mode="local",
                json_repair_attempts=1,
            ),
        )
        assert rule.rule_id is not None
        assert len(rule.rule_id) == 36  # UUID4
        assert rule.confidence == 0.95
        assert rule.version == 1
        assert rule.approved_by is None

    def test_excerpt_auto_truncated(self):
        """SourceRef excerpt longer than 100 chars should be truncated."""
        ref = SourceRef(
            chunk_id="c1",
            source_type="slack",
            excerpt="A" * 200,
        )
        assert len(ref.excerpt) == 100

    def test_rejected_rule_allows_low_confidence(self):
        """Rejected rules can have any confidence (no source requirement enforced)."""
        rule = BusinessRule(
            rule_text="Refunds processed within 48 hours for all customers.",
            rule_category="other",
            confidence=0.2,
            verification_status="rejected",
            source_refs=[],
            extraction_meta=self._valid_meta(),
        )
        assert rule.verification_status == "rejected"


class TestRulesFile:
    """Tests for the RulesFile container and YAML output."""

    def test_to_yaml_produces_parseable_output(self):
        """RulesFile.to_yaml() must produce valid YAML that round-trips."""
        rule = BusinessRule(
            rule_text="Refunds processed within 48 hours.",
            rule_category="other",
            confidence=0.9,
            verification_status="verified",
            source_refs=[SourceRef(chunk_id="c1", source_type="slack")],
            extraction_meta=ExtractionMeta(provider="test", model="m", deployment_mode="local"),
        )
        rules_file = RulesFile(company_id="acme-corp", rules=[rule])
        yaml_str = rules_file.to_yaml()

        # Must be parseable
        parsed = yaml.safe_load(yaml_str)
        assert parsed["company_id"] == "acme-corp"
        assert parsed["total_rules"] == 1
        assert len(parsed["rules"]) == 1
        assert parsed["rules"][0]["rule_text"] == "Refunds processed within 48 hours."

    def test_total_rules_computed(self):
        """total_rules property should reflect len(rules)."""
        rule = BusinessRule(
            rule_text="Refunds processed within 48 hours.",
            rule_category="other",
            confidence=0.9,
            verification_status="verified",
            source_refs=[SourceRef(chunk_id="c1", source_type="slack")],
            extraction_meta=ExtractionMeta(provider="test", model="m", deployment_mode="local"),
        )
        rules_file = RulesFile(company_id="test", rules=[rule, rule])
        assert rules_file.total_rules == 2
