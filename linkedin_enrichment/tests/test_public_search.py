"""HTML parsing for the key-free public backends.

The fixtures below are **hand-written to match the documented structure** of each
endpoint — unlike the SERP fixtures in ``fixtures/serp/``, they are not verbatim
recordings, because the build environment cannot reach these hosts. They are
therefore a test of the parser's logic (redirect unwrapping, engine-host
filtering, snippet capture), not proof that the live markup is unchanged.

``main.py preflight`` is what verifies the live markup, and it reports
INCONCLUSIVE rather than "no results" when a parser stops matching — so a markup
drift surfaces as a loud failure instead of a silently empty run.
"""

from __future__ import annotations

import pytest

from linkedin_enrichment.providers.public_search import (
    DdgHtmlBackend,
    DdgLiteBackend,
    MojeekBackend,
    SearxngJsonBackend,
    _unwrap_redirect,
    looks_like_policy_denial,
)

# DuckDuckGo wraps every result URL in its own redirect and links back to itself
# in the surrounding chrome — both must be handled.
DDG_HTML = """
<html><body>
  <a class="header__logo" href="/">DuckDuckGo</a>
  <div class="result results_links">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fgirishrowjee%2F&amp;rut=abc">
       Girish Rowjee - Greytip Software Pvt. Ltd. | LinkedIn</a>
    Bengaluru, Karnataka, India &middot; Co-Founder &amp; CEO
  </div>
  <div class="result results_links">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fin.linkedin.com%2Fin%2Fakshay-kumar-b977b08b">
       Akshay Kumar - BDE @ Greytip Software | LinkedIn</a>
    Bengaluru
  </div>
  <a href="https://duckduckgo.com/settings">Settings</a>
</body></html>
"""

MOJEEK_HTML = """
<html><body>
  <a href="/">Mojeek</a>
  <ul class="results-standard">
    <li><h2><a href="https://www.linkedin.com/in/girishrowjee/">
        Girish Rowjee - Greytip Software Pvt. Ltd.</a></h2>
        <p class="s">Co-Founder and CEO at Greytip Software, Bengaluru.</p></li>
  </ul>
  <a href="https://www.mojeek.com/about">About</a>
</body></html>
"""

# A consent/challenge page: HTTP 200, no results. Must not read as "no matches".
EMPTY_HTML = "<html><body><p>No results found.</p></body></html>"


class TestRedirectUnwrapping:
    @pytest.mark.parametrize("href,expected", [
        ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fabc&rut=x",
         "https://www.linkedin.com/in/abc"),
        ("https://www.linkedin.com/in/plain", "https://www.linkedin.com/in/plain"),
        ("/relative/path", ""),
        ("", ""),
    ])
    def test_unwrap(self, href: str, expected: str) -> None:
        assert _unwrap_redirect(href) == expected


class TestDdgHtml:
    def test_extracts_linkedin_results(self) -> None:
        results = DdgHtmlBackend().parse(DDG_HTML, limit=10)
        urls = [r.url for r in results]
        assert "https://www.linkedin.com/in/girishrowjee/" in urls
        assert "https://in.linkedin.com/in/akshay-kumar-b977b08b" in urls

    def test_engine_own_links_are_dropped(self) -> None:
        """Without this the engine's own chrome becomes bogus candidates."""
        results = DdgHtmlBackend().parse(DDG_HTML, limit=10)
        assert not any("duckduckgo.com" in r.url for r in results)

    def test_title_and_snippet_captured(self) -> None:
        top = DdgHtmlBackend().parse(DDG_HTML, limit=10)[0]
        assert "Girish Rowjee" in top.title
        assert "Greytip Software" in top.title
        # The snippet carries the location evidence the scorer uses.
        assert "Bengaluru" in top.snippet

    def test_limit_is_honoured(self) -> None:
        assert len(DdgHtmlBackend().parse(DDG_HTML, limit=1)) == 1

    def test_empty_page_yields_nothing(self) -> None:
        assert DdgHtmlBackend().parse(EMPTY_HTML, limit=10) == []

    def test_request_shape(self) -> None:
        backend = DdgHtmlBackend()
        assert backend.method == "POST"
        assert backend.build("some query", 10)["data"]["q"] == "some query"


class TestDdgLite:
    def test_parses_same_structure(self) -> None:
        results = DdgLiteBackend().parse(DDG_HTML, limit=10)
        assert any("linkedin.com/in/" in r.url for r in results)


class TestMojeek:
    def test_extracts_results_and_skips_own_links(self) -> None:
        results = MojeekBackend().parse(MOJEEK_HTML, limit=10)
        assert results[0].url == "https://www.linkedin.com/in/girishrowjee/"
        assert not any("mojeek.com" in r.url for r in results)

    def test_request_shape(self) -> None:
        assert MojeekBackend().build("q", 10)["params"]["q"] == "q"


class TestSearxngBackend:
    def test_parses_json(self) -> None:
        body = (
            '{"results": [{"title": "Girish Rowjee - Greytip", '
            '"url": "https://www.linkedin.com/in/girishrowjee/", "content": "Bengaluru"}]}'
        )
        results = SearxngJsonBackend().parse(body, limit=10)
        assert len(results) == 1
        assert results[0].snippet == "Bengaluru"

    def test_html_response_reports_the_actual_misconfiguration(self) -> None:
        """A SearXNG serving HTML means `json` is missing from search.formats.
        Saying so beats a generic parse error."""
        from linkedin_enrichment.providers.base import FailureKind, ProviderError

        with pytest.raises(ProviderError) as excinfo:
            SearxngJsonBackend().parse("<html>not json</html>", limit=10)
        assert excinfo.value.kind is FailureKind.CONFIG
        assert "settings.yml" in str(excinfo.value)


class TestProviderComposition:
    def test_searxng_backend_omitted_without_a_url(self, settings) -> None:
        from linkedin_enrichment.providers.public_search import PublicSearchProvider

        settings.provider.searxng_url = ""
        provider = PublicSearchProvider(settings.provider)
        assert "searxng" not in [b.name for b in provider.backends]

    def test_searxng_backend_included_when_configured(self, settings) -> None:
        from linkedin_enrichment.providers.public_search import PublicSearchProvider

        settings.provider.searxng_url = "http://localhost:8080"
        provider = PublicSearchProvider(settings.provider)
        assert "searxng" in [b.name for b in provider.backends]

    def test_html_backends_are_not_authoritative_on_empty(self, settings) -> None:
        """The property that protects the negative cache."""
        from linkedin_enrichment.providers.public_search import PublicSearchProvider

        assert PublicSearchProvider(settings.provider).empty_means_absent is False

    def test_policy_denial_helper_is_exported(self) -> None:
        assert looks_like_policy_denial("host not in allowlist: x", None)
