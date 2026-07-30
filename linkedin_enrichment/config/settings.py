"""Single source of truth for every tunable in the pipeline.

Resolution order (lowest to highest precedence):
    dataclass defaults  ->  defaults.yaml  ->  environment variables  ->  CLI flags

Nothing else in the codebase should read ``os.environ`` directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any, Mapping

import yaml

# Env vars are namespaced so they cannot collide with unrelated tooling.
ENV_PREFIX = "LE_"


@dataclass
class ScoringWeights:
    """Feature weights for the identity scorer. Must sum to 1.0.

    ``core_conjunction`` is ``name_similarity * company_coverage`` — a *product*,
    not two separate addends. This is the single most important modelling choice
    in the pipeline. Live validation showed that name alone and company alone each
    produce confident-looking false positives ("Debasheesh Bagchi" at the wrong
    company; ten real colleagues at the right company, none of them the subject).
    Only the conjunction is safe, and a product means either factor collapsing
    takes the whole score with it.

    The remaining weights are corroboration: they can lift a good core match over
    the line, but can never carry a candidate on their own.
    """

    core_conjunction: float = 0.70   # name_similarity * company_coverage
    slug_agreement: float = 0.10
    location_match: float = 0.08
    country_subdomain: float = 0.05
    industry_match: float = 0.04
    designation_match: float = 0.03

    def total(self) -> float:
        return sum(getattr(self, f.name) for f in fields(self))


@dataclass
class CalibrationConfig:
    """Maps a raw score in [0, 1] to a calibrated probability.

    A hand-weighted sum is *not* a probability, so comparing it directly against
    the 95% requirement would be meaningless. The shipped default is a logistic
    curve whose midpoint and slope were chosen against the validated cases:

    ==========================================  ========  ===========
    Evidence                                    raw       confidence
    ==========================================  ========  ===========
    exact name + exact company, nothing else    0.70      0.964
    the above + URL slug + city                 0.88      0.999
    good-but-imperfect name + exact company     0.63      0.853  (review queue)
    company absent from the result              <=0.30    ~0      (rejected outright)
    ==========================================  ========  ===========

    ``main.py calibrate`` replaces this with an isotonic fit over hand-labelled
    data; ``isotonic_points`` then takes precedence over the logistic parameters.
    """

    midpoint: float = 0.55      # raw score mapping to P = 0.5
    slope: float = 22.0         # higher = sharper transition
    #: Fitted (raw, probability) knots from ``calibrate``; empty = use the logistic.
    isotonic_points: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class RejectRules:
    """Hard gates applied *after* scoring. This is where precision comes from.

    Each default below was chosen against six real search results captured in
    ``tests/fixtures/serp/`` — see README "Why these thresholds".
    """

    # A candidate must carry the company's brand tokens in its title or snippet.
    # Without this, "right name at the wrong company" scores high and is written
    # as a confident match (validated failure case: Debasheesh Bagchi).
    require_company_token: bool = True
    min_company_token_coverage: float = 0.60

    # A candidate must look like the right person. Without this, a company-roster
    # query returns 10 real colleagues and the top one wins (validated failure
    # case: Melukote Shivaramu Lokesh / Synthesis Winding).
    min_name_similarity: float = 0.82

    # Top-1 must beat top-2 by this margin, else the row is ambiguous and blanked
    # (validated failure case: 8 different "Praveen Kumar" profiles).
    min_margin: float = 0.08

    # A LinkedIn URL that wins for two different people is trusted for neither.
    reject_shared_url: bool = True

    # Non-profile LinkedIn URL path segments that can never be a person.
    non_profile_markers: tuple[str, ...] = (
        "/company/", "/school/", "/pub/dir/", "/posts/", "/jobs/",
        "/showcase/", "/groups/", "/learning/", "/pulse/", "/events/",
        "/newsletters/", "/products/", "/services/",
    )


@dataclass
class RateLimitConfig:
    """Token bucket plus adaptive backpressure.

    Free providers (SearXNG scraping upstream engines, DuckDuckGo) will throttle
    under sustained load, so the bucket *shrinks itself* on throttle signals and
    recovers slowly. Defaults are deliberately polite.
    """

    requests_per_second: float = 0.5      # 1 query every 2s — safe for free providers
    burst: int = 2
    # On HTTP 429 / DDG "202 Ratelimit", multiply the refill rate by this.
    throttle_backoff_factor: float = 0.5
    # Per successful request, nudge the rate back up by this factor (capped at the configured max).
    recovery_factor: float = 1.02
    min_requests_per_second: float = 0.05


@dataclass
class RetryConfig:
    max_attempts: int = 4
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 120.0
    jitter: float = 0.3
    # Transient -> retry. Everything else fails fast (a 401 will never fix itself).
    retry_status_codes: tuple[int, ...] = (202, 408, 425, 429, 500, 502, 503, 504)


@dataclass
class ProviderConfig:
    # public_search needs no key, no account and no self-hosting, so a fresh
    # checkout can attempt a real search immediately. It is a composite over
    # several key-free endpoints; see providers/public_search.py.
    name: str = "public_search"
    #: Backend order for public_search — first one to return results wins.
    public_backends: tuple[str, ...] = ("ddg_html", "ddg_lite", "mojeek", "searxng")
    #: Only used when a SearXNG instance is available (as a public_search
    #: backend, or on its own via `--provider searxng`).
    searxng_url: str = ""
    google_cse_id: str = ""
    google_api_key: str = ""
    serper_api_key: str = ""
    serpapi_api_key: str = ""
    cassette_dir: str = ""                # set by tests / offline replay
    timeout_seconds: float = 30.0
    results_per_query: int = 10
    user_agent: str = "gcia-linkedin-enrichment/1.0"


@dataclass
class LLMConfig:
    """Optional adjudication of borderline candidates. Off by default: it costs
    money and the deterministic scorer must stand on its own."""

    enabled: bool = False
    model: str = "claude-sonnet-5"
    api_key: str = ""
    # Only candidates scoring inside this band are sent to the model.
    band_low: float = 0.80
    band_high: float = 0.95
    max_tokens: int = 512


@dataclass
class Settings:
    """Top-level configuration object passed explicitly through the pipeline."""

    # --- paths ---
    input_paths: list[str] = field(default_factory=list)
    output_dir: str = "out"
    db_path: str = "out/enrichment.db"
    log_dir: str = "logs"

    # --- execution ---
    workers: int = 4
    batch_size: int = 200
    # A row claimed but not completed within this window is presumed abandoned
    # (crashed worker) and returned to the pending pool on the next startup.
    stale_claim_seconds: float = 900.0

    # --- decisioning ---
    confidence_threshold: float = 0.95
    # Candidates below the threshold but above this are written to review_queue.csv
    # rather than discarded. They are NEVER written to the LinkedIn Profile column.
    review_queue_floor: float = 0.55

    # --- query ladder ---
    enable_company_tier: bool = True      # Tier 1: one roster query per company
    enable_person_tier: bool = True       # Tier 2: per-person query
    enable_negative_cache: bool = True    # Tier 1b: skip everyone at footprint-less companies
    serp_cache_ttl_days: int = 30

    # --- nested ---
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    reject: RejectRules = field(default_factory=RejectRules)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    # --- misc ---
    log_level: str = "INFO"
    dashboard: bool = True
    run_id: str = ""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> "Settings":
        """Build settings from defaults.yaml, then env vars, then explicit overrides."""
        settings = cls()

        path = Path(config_path) if config_path else Path(__file__).parent / "defaults.yaml"
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            settings._apply_mapping(raw)

        settings._apply_env()

        if overrides:
            # CLI wins over everything; drop None so unset flags don't clobber config.
            settings._apply_mapping({k: v for k, v in overrides.items() if v is not None})

        settings._validate()
        return settings

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _apply_mapping(self, data: Mapping[str, Any]) -> None:
        """Apply a (possibly nested) mapping onto this instance."""
        nested = {f.name for f in fields(self) if hasattr(getattr(self, f.name), "__dataclass_fields__")}
        for key, value in data.items():
            if key in nested and isinstance(value, Mapping):
                target = getattr(self, key)
                for sub_key, sub_value in value.items():
                    if hasattr(target, sub_key):
                        setattr(target, sub_key, sub_value)
            elif hasattr(self, key):
                setattr(self, key, value)

    def _apply_env(self) -> None:
        """Read LE_*-prefixed env vars, plus a few conventional provider keys.

        ``LE_WORKERS=8`` sets ``workers``; ``LE_PROVIDER__NAME=serper`` sets the
        nested ``provider.name`` (double underscore is the nesting separator).
        """
        for raw_key, raw_value in os.environ.items():
            if not raw_key.startswith(ENV_PREFIX):
                continue
            path = raw_key[len(ENV_PREFIX):].lower().split("__")
            target: Any = self
            for part in path[:-1]:
                if not hasattr(target, part):
                    target = None
                    break
                target = getattr(target, part)
            if target is None or not hasattr(target, path[-1]):
                continue
            current = getattr(target, path[-1])
            setattr(target, path[-1], _coerce(raw_value, current))

        # Conventional names, so users don't have to learn our prefix for secrets.
        for env_name, (obj_name, attr) in {
            "SEARXNG_URL": ("provider", "searxng_url"),
            "GOOGLE_API_KEY": ("provider", "google_api_key"),
            "GOOGLE_CSE_ID": ("provider", "google_cse_id"),
            "SERPER_API_KEY": ("provider", "serper_api_key"),
            "SERPAPI_API_KEY": ("provider", "serpapi_api_key"),
            "ANTHROPIC_API_KEY": ("llm", "api_key"),
        }.items():
            value = os.environ.get(env_name)
            if value:
                setattr(getattr(self, obj_name), attr, value)

    def _validate(self) -> None:
        if not 0.0 < self.confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be in (0, 1]")
        if self.review_queue_floor > self.confidence_threshold:
            raise ValueError("review_queue_floor must be <= confidence_threshold")
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        if self.rate_limit.requests_per_second <= 0:
            raise ValueError("rate_limit.requests_per_second must be > 0")
        total = self.weights.total()
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"scoring weights must sum to 1.0, got {total:.4f}")

    def to_dict(self) -> dict[str, Any]:
        """Serialisable snapshot, with secrets redacted — safe to log."""
        data = asdict(self)
        for section, key in (
            ("provider", "google_api_key"), ("provider", "serper_api_key"),
            ("provider", "serpapi_api_key"), ("llm", "api_key"),
        ):
            if data.get(section, {}).get(key):
                data[section][key] = "***redacted***"
        return data


def _coerce(raw: str, current: Any) -> Any:
    """Coerce an env-var string to the type of the value it replaces."""
    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, (list, tuple)):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        # Preserve the element type of the existing value, so a numeric setting
        # such as retry_status_codes does not silently become a tuple of strings
        # and stop matching the integer status codes it is compared against.
        if current and all(isinstance(item, int) for item in current):
            try:
                parts = [int(p) for p in parts]
            except ValueError:
                pass
        return type(current)(parts)
    return raw
