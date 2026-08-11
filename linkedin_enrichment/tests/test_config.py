"""Configuration loading, precedence and validation."""

from __future__ import annotations

import pytest

from linkedin_enrichment.config.settings import Settings


class TestDefaults:
    def test_defaults_are_valid(self) -> None:
        config = Settings.load()
        assert config.confidence_threshold == 0.95
        assert config.provider.name == "public_search", (
            "the key-free provider must be the default"
        )
        assert config.provider.public_backends, "public_search needs a backend order"
        assert config.llm.enabled is False, "the LLM stage must be opt-in"

    def test_weights_sum_to_one(self) -> None:
        assert Settings.load().weights.total() == pytest.approx(1.0)


class TestValidation:
    def test_rejects_weights_that_do_not_sum_to_one(self) -> None:
        with pytest.raises(ValueError, match="weights must sum"):
            Settings.load(overrides={"weights": {"core_conjunction": 0.9}})

    def test_rejects_review_floor_above_threshold(self) -> None:
        with pytest.raises(ValueError, match="review_queue_floor"):
            Settings.load(overrides={"review_queue_floor": 0.99, "confidence_threshold": 0.95})

    @pytest.mark.parametrize("bad", [0.0, -1.0, 1.5])
    def test_rejects_impossible_thresholds(self, bad: float) -> None:
        with pytest.raises(ValueError, match="confidence_threshold"):
            Settings.load(overrides={"confidence_threshold": bad})

    def test_rejects_zero_workers(self) -> None:
        with pytest.raises(ValueError, match="workers"):
            Settings.load(overrides={"workers": 0})


class TestEnvironment:
    def test_prefixed_scalar(self, monkeypatch) -> None:
        monkeypatch.setenv("LE_WORKERS", "12")
        assert Settings.load().workers == 12

    def test_nested_via_double_underscore(self, monkeypatch) -> None:
        monkeypatch.setenv("LE_PROVIDER__NAME", "serper")
        monkeypatch.setenv("LE_RATE_LIMIT__REQUESTS_PER_SECOND", "3.5")
        config = Settings.load()
        assert config.provider.name == "serper"
        assert config.rate_limit.requests_per_second == 3.5

    def test_boolean_coercion(self, monkeypatch) -> None:
        monkeypatch.setenv("LE_DASHBOARD", "false")
        assert Settings.load().dashboard is False

    def test_numeric_tuple_keeps_its_element_type(self, monkeypatch) -> None:
        """Regression: string elements would never match an integer status code."""
        monkeypatch.setenv("LE_RETRY__RETRY_STATUS_CODES", "429,503")
        codes = Settings.load().retry.retry_status_codes
        assert codes == (429, 503)
        assert all(isinstance(code, int) for code in codes)

    def test_conventional_secret_names(self, monkeypatch) -> None:
        monkeypatch.setenv("SEARXNG_URL", "http://example.invalid:8080")
        monkeypatch.setenv("SERPER_API_KEY", "secret-value")
        config = Settings.load()
        assert config.provider.searxng_url == "http://example.invalid:8080"
        assert config.provider.serper_api_key == "secret-value"

    def test_cli_overrides_beat_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("LE_WORKERS", "12")
        assert Settings.load(overrides={"workers": 3}).workers == 3

    def test_none_overrides_are_ignored(self, monkeypatch) -> None:
        """Unset CLI flags arrive as None and must not clobber configured values."""
        monkeypatch.setenv("LE_WORKERS", "12")
        assert Settings.load(overrides={"workers": None}).workers == 12


class TestRedaction:
    def test_secrets_are_redacted_in_snapshots(self) -> None:
        config = Settings.load(overrides={"provider": {"serper_api_key": "super-secret"}})
        snapshot = config.to_dict()
        assert snapshot["provider"]["serper_api_key"] == "***redacted***"
        assert "super-secret" not in str(snapshot)
