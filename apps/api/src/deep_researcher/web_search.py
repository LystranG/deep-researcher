from typing import Protocol, TypedDict

import httpx


class SearchResult(TypedDict):
    title: str
    url: str
    snippet: str


class SearchUnavailableError(RuntimeError):
    pass


class WebSearchGateway(Protocol):
    def search(self, query: str, *, count: int = 5) -> list[SearchResult]: ...


class DisabledWebSearchGateway:
    def search(self, query: str, *, count: int = 5) -> list[SearchResult]:
        del query, count
        raise SearchUnavailableError("未配置 Brave Search API 凭证")


class BraveWebSearchGateway:
    """Brave Web Search API 的最小生产 Adapter。"""

    def __init__(self, *, api_key: str, client: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=15.0)

    def search(self, query: str, *, count: int = 5) -> list[SearchResult]:
        try:
            response = self._client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._api_key,
                },
                params={"q": query, "count": max(1, min(count, 20))},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchUnavailableError("Brave 搜索暂时不可用") from exc

        raw_results = payload.get("web", {}).get("results", [])
        results: list[SearchResult] = []
        for item in raw_results:
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            snippet = str(item.get("description", "")).strip()
            if title and url and snippet:
                results.append({"title": title, "url": url, "snippet": snippet})
        return results


def build_web_search_gateway(*, api_key: str | None) -> WebSearchGateway:
    if not api_key:
        return DisabledWebSearchGateway()
    return BraveWebSearchGateway(api_key=api_key)
