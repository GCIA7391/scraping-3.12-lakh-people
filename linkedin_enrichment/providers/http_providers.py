"""HTTP-based search adapters.

They share one aiohttp session-management pattern, so they live together rather
than in several near-identical files.

Cost and cap figures below drive ``main.py estimate``; they were checked while
building this pipeline and should be re-checked before a large paid run, since
SERP vendors change pricing often.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import ProviderError, SearchProvider, SearchResponse, SerpResult

logger = logging.getLogger(__name__)


class _AiohttpProvider(SearchProvider):
    """Shared session lifecycle and error classification."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._session: Any = None
        self._session_lock = asyncio.Lock()

    async def _get_session(self):
        import aiohttp

        # Double-checked under a lock: several workers start concurrently and
        # would otherwise each build (and leak) their own session.
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
                    self._session = aiohttp.ClientSession(
                        timeout=timeout,
                        headers={"User-Agent": self.config.user_agent},
                    )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _request_json(
        self, method: str, url: str, **kwargs: Any
    ) -> dict[str, Any]:
        import aiohttp

        session = await self._get_session()
        try:
            async with session.request(method, url, **kwargs) as response:
                # 202 is DuckDuckGo/SearXNG's soft throttle; 429 is the standard one.
                if response.status in (202, 429) or response.status >= 500:
                    raise ProviderError(
                        f"{self.name}: HTTP {response.status}",
                        retryable=True, status=response.status,
                    )
                if response.status >= 400:
                    body = (await response.text())[:200]
                    raise ProviderError(
                        f"{self.name}: HTTP {response.status}: {body}",
                        retryable=False, status=response.status,
                    )
                return await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise ProviderError(f"{self.name}: timeout", retryable=True) from exc
        except aiohttp.ClientError as exc:
            raise ProviderError(f"{self.name}: {exc}", retryable=True) from exc


class SearxngProvider(_AiohttpProvider):
    """Self-hosted SearXNG — the default, because it is genuinely free.

    SearXNG proxies real search engines, so the instance itself is what gets
    rate-limited or CAPTCHA'd upstream. Keep the configured request rate low and
    prefer an IP outside the big cloud ranges, which upstream engines block
    fastest. The pipeline's checkpoint/resume exists largely to make a slow,
    interruptible run over this provider practical.
    """

    name = "searxng"
    cost_per_1k = 0.0

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        base = (self.config.searxng_url or "").rstrip("/")
        if not base:
            raise ProviderError("searxng_url is not configured", retryable=False)
        payload = await self._request_json(
            "GET", f"{base}/search",
            params={"q": query, "format": "json", "language": "en", "safesearch": "0"},
        )
        results = [
            SerpResult(
                title=item.get("title", "") or "",
                url=item.get("url", "") or "",
                snippet=item.get("content", "") or "",
                rank=index,
            )
            for index, item in enumerate(payload.get("results", [])[:limit])
        ]
        return SearchResponse(query=query, results=results, provider=self.name)

    def validate(self) -> None:
        if not (self.config.searxng_url or "").strip():
            raise ProviderError(
                "SearXNG selected but no URL set. Start one with "
                "`docker run -p 8080:8080 searxng/searxng` and set SEARXNG_URL.",
                retryable=False,
            )


class GoogleCseProvider(_AiohttpProvider):
    """Google Programmable Search (Custom Search JSON API).

    Free tier is 100 queries/day, which is negligible against 312k rows; beyond
    that it is $5 per 1,000 with a 10,000/day ceiling. Included because it is the
    most ToS-clean option and is useful for calibration sampling.
    """

    name = "google_cse"
    cost_per_1k = 5.0
    daily_query_cap = 10_000

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        payload = await self._request_json(
            "GET", "https://www.googleapis.com/customsearch/v1",
            params={
                "key": self.config.google_api_key,
                "cx": self.config.google_cse_id,
                "q": query,
                "num": str(min(limit, 10)),
            },
        )
        results = [
            SerpResult(
                title=item.get("title", "") or "",
                url=item.get("link", "") or "",
                snippet=item.get("snippet", "") or "",
                rank=index,
            )
            for index, item in enumerate(payload.get("items", [])[:limit])
        ]
        return SearchResponse(query=query, results=results, provider=self.name)

    def validate(self) -> None:
        if not self.config.google_api_key or not self.config.google_cse_id:
            raise ProviderError(
                "google_cse requires GOOGLE_API_KEY and GOOGLE_CSE_ID", retryable=False
            )


class SerperProvider(_AiohttpProvider):
    """Serper.dev — cheapest paid Google SERP API, high throughput, no daily cap."""

    name = "serper"
    cost_per_1k = 1.0

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        payload = await self._request_json(
            "POST", "https://google.serper.dev/search",
            json={"q": query, "num": min(limit, 10), "gl": "in"},
            headers={
                "X-API-KEY": self.config.serper_api_key,
                "Content-Type": "application/json",
            },
        )
        results = [
            SerpResult(
                title=item.get("title", "") or "",
                url=item.get("link", "") or "",
                snippet=item.get("snippet", "") or "",
                rank=index,
            )
            for index, item in enumerate(payload.get("organic", [])[:limit])
        ]
        return SearchResponse(query=query, results=results, provider=self.name)

    def validate(self) -> None:
        if not self.config.serper_api_key:
            raise ProviderError("serper requires SERPER_API_KEY", retryable=False)


class SerpApiProvider(_AiohttpProvider):
    """SerpApi — most robust parsing and block handling, highest cost."""

    name = "serpapi"
    cost_per_1k = 15.0

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        payload = await self._request_json(
            "GET", "https://serpapi.com/search",
            params={
                "api_key": self.config.serpapi_api_key,
                "q": query, "engine": "google", "num": str(min(limit, 10)), "gl": "in",
            },
        )
        results = [
            SerpResult(
                title=item.get("title", "") or "",
                url=item.get("link", "") or "",
                snippet=item.get("snippet", "") or "",
                rank=index,
            )
            for index, item in enumerate(payload.get("organic_results", [])[:limit])
        ]
        return SearchResponse(query=query, results=results, provider=self.name)

    def validate(self) -> None:
        if not self.config.serpapi_api_key:
            raise ProviderError("serpapi requires SERPAPI_API_KEY", retryable=False)


class DuckDuckGoProvider(SearchProvider):
    """Free DuckDuckGo access via the ``ddgs`` package (formerly duckduckgo_search).

    No API key, but DuckDuckGo throttles hard under sustained automation and
    answers with a ``202 Ratelimit``. Treated as retryable so the adaptive rate
    limiter backs off rather than failing the row. Best used as a fallback when
    a SearXNG instance is unavailable.
    """

    name = "duckduckgo"
    cost_per_1k = 0.0

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        def _blocking() -> list[dict[str, Any]]:
            try:
                from ddgs import DDGS
            except ImportError:  # pragma: no cover - optional dependency
                try:
                    from duckduckgo_search import DDGS  # type: ignore[no-redef]
                except ImportError as exc:
                    raise ProviderError(
                        "duckduckgo provider needs `pip install ddgs`", retryable=False
                    ) from exc
            with DDGS() as client:
                return list(client.text(query, max_results=limit))

        try:
            # ddgs is synchronous; keep it off the event loop.
            raw = await asyncio.to_thread(_blocking)
        except ProviderError:
            raise
        except Exception as exc:
            message = str(exc)
            retryable = "ratelimit" in message.lower() or "202" in message
            raise ProviderError(f"duckduckgo: {message}", retryable=retryable) from exc

        results = [
            SerpResult(
                title=item.get("title", "") or "",
                url=item.get("href", item.get("link", "")) or "",
                snippet=item.get("body", "") or "",
                rank=index,
            )
            for index, item in enumerate(raw[:limit])
        ]
        return SearchResponse(query=query, results=results, provider=self.name)
