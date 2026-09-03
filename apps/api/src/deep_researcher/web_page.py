import hashlib
import ipaddress
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from html.parser import HTMLParser
from typing import Protocol, TypedDict, cast
from urllib.parse import urljoin, urlparse

import httpx

_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_MUST_STOP_CATEGORIES = {
    "cancelled",
    "unsafe_url",
    "policy_rejected",
    "acl_rejected",
    "constraint_exhausted",
}


class FetchedWebPage(TypedDict):
    """受限网页抓取后可持久化的正文"""

    title: str
    content: str
    truncated: bool


@dataclass(frozen=True)
class WebPageAttempt:
    """记录单个正文 Adapter 的规范化获取结果"""

    adapter_id: str
    adapter_version: str
    requested_url: str
    final_url: str | None
    status: str
    http_status: int | None
    content_type: str | None
    warning_category: str | None
    error_category: str | None
    retryable: bool
    completeness: str
    truncated: bool
    content_hash: str | None
    title: str | None
    content: str | None
    fallback_allowed: bool

    @classmethod
    def success(
        cls,
        *,
        adapter_id: str,
        adapter_version: str,
        requested_url: str,
        final_url: str,
        http_status: int | None,
        content_type: str | None,
        title: str,
        content: str,
        complete: bool,
        truncated: bool,
        warning_category: str | None = None,
    ) -> "WebPageAttempt":
        """构造可持久化正文的成功尝试"""
        return cls(
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            requested_url=requested_url,
            final_url=final_url,
            status="success",
            http_status=http_status,
            content_type=content_type,
            warning_category=warning_category,
            error_category=None,
            retryable=False,
            completeness="complete" if complete else "partial",
            truncated=truncated,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            title=title,
            content=content,
            fallback_allowed=False,
        )

    @classmethod
    def failure(
        cls,
        *,
        adapter_id: str,
        adapter_version: str,
        requested_url: str,
        final_url: str | None = None,
        http_status: int | None = None,
        content_type: str | None = None,
        error_category: str,
        retryable: bool,
        fallback_allowed: bool,
        warning_category: str | None = None,
    ) -> "WebPageAttempt":
        """构造不产生正文快照的失败尝试"""
        return cls(
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            requested_url=requested_url,
            final_url=final_url,
            status="failed",
            http_status=http_status,
            content_type=content_type,
            warning_category=warning_category,
            error_category=error_category,
            retryable=retryable,
            completeness="none",
            truncated=False,
            content_hash=None,
            title=None,
            content=None,
            fallback_allowed=fallback_allowed,
        )


@dataclass(frozen=True)
class WebAcquisitionResult:
    """向调用方返回正文选择与全部 Adapter 尝试"""

    selected: WebPageAttempt | None
    attempts: tuple[WebPageAttempt, ...]
    stopped_reason: str | None = None


class WebPageGateway(Protocol):
    """读取单个公开网页正文的受限 Adapter"""

    def fetch(self, url: str) -> WebPageAttempt | FetchedWebPage | None: ...


class WebAcquisitionGateway(Protocol):
    """通过统一 Interface 获取正文与审计尝试"""

    def acquire(
        self, url: str, *, should_stop: Callable[[], bool] | None = None
    ) -> WebAcquisitionResult: ...


class WebAcquisition:
    """集中执行 URL 安全校验、Provider 主读取与 Local HTTP fallback"""

    def __init__(
        self,
        *,
        firecrawl_reader: WebPageGateway | None = None,
        local_reader: WebPageGateway | None,
        jina_reader: WebPageGateway | None = None,
        url_validator: Callable[[str], None] | None = None,
    ) -> None:
        """注入正文 Provider、兼容旧 Jina 调用方与公共 URL 校验函数"""
        if firecrawl_reader is None and jina_reader is None:
            raise ValueError("必须配置网页正文 Provider")
        self._provider_reader = firecrawl_reader or jina_reader
        assert self._provider_reader is not None
        self._provider_adapter_id = (
            "firecrawl_reader" if firecrawl_reader is not None else "jina_reader"
        )
        self._local_reader = local_reader
        self._url_validator = url_validator or _validate_public_url

    def acquire(
        self, url: str, *, should_stop: Callable[[], bool] | None = None
    ) -> WebAcquisitionResult:
        """按安全策略选择正文并保留每次尝试"""
        try:
            self._url_validator(url)
        except ValueError:
            return WebAcquisitionResult(selected=None, attempts=(), stopped_reason="unsafe_url")
        if should_stop is not None and should_stop():
            return WebAcquisitionResult(selected=None, attempts=(), stopped_reason="cancelled")

        provider_reader = self._provider_reader
        assert provider_reader is not None
        provider_attempt = _normalize_attempt(
            provider_reader.fetch(url),
            adapter_id=self._provider_adapter_id,
            adapter_version="legacy",
            requested_url=url,
        )
        provider_attempt = _reject_unsafe_final_url(provider_attempt, self._url_validator)
        attempts = [provider_attempt]
        if provider_attempt.status == "success":
            return WebAcquisitionResult(selected=provider_attempt, attempts=tuple(attempts))
        if (
            provider_attempt.error_category in _MUST_STOP_CATEGORIES
            or not provider_attempt.fallback_allowed
            or self._local_reader is None
        ):
            return WebAcquisitionResult(
                selected=None,
                attempts=tuple(attempts),
                stopped_reason=provider_attempt.error_category,
            )
        if should_stop is not None and should_stop():
            return WebAcquisitionResult(
                selected=None, attempts=tuple(attempts), stopped_reason="cancelled"
            )
        local_attempt = _normalize_attempt(
            self._local_reader.fetch(url),
            adapter_id="local_http",
            adapter_version="legacy",
            requested_url=url,
        )
        local_attempt = _reject_unsafe_final_url(local_attempt, self._url_validator)
        attempts.append(local_attempt)
        return WebAcquisitionResult(
            selected=local_attempt if local_attempt.status == "success" else None,
            attempts=tuple(attempts),
            stopped_reason=(
                None if local_attempt.status == "success" else local_attempt.error_category
            ),
        )


class FirecrawlWebPageAdapter:
    """将 Firecrawl Scrape API 的 Markdown 响应规范化为正文获取尝试。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        adapter_version: str = "v2",
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=60.0, follow_redirects=False)
        self._adapter_version = adapter_version

    def fetch(self, url: str) -> WebPageAttempt:
        """通过 Firecrawl 抓取单个公开 URL，并只请求 Markdown 格式。"""
        if not self._api_key:
            return WebPageAttempt.failure(
                adapter_id="firecrawl_reader",
                adapter_version=self._adapter_version,
                requested_url=url,
                error_category="provider_unavailable",
                retryable=False,
                fallback_allowed=True,
            )
        try:
            response = self._client.post(
                "https://api.firecrawl.dev/v2/scrape",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            )
            if response.status_code >= 400:
                return WebPageAttempt.failure(
                    adapter_id="firecrawl_reader",
                    adapter_version=self._adapter_version,
                    requested_url=url,
                    http_status=response.status_code,
                    content_type=response.headers.get("content-type"),
                    error_category=_firecrawl_error_category(response),
                    retryable=response.status_code in {408, 429} or response.status_code >= 500,
                    fallback_allowed=response.status_code not in {401, 402, 403},
                )
            raw_payload = response.json()
            if not isinstance(raw_payload, dict):
                return WebPageAttempt.failure(
                    adapter_id="firecrawl_reader",
                    adapter_version=self._adapter_version,
                    requested_url=url,
                    http_status=response.status_code,
                    error_category="unusable_extraction",
                    retryable=False,
                    fallback_allowed=True,
                )
            payload = cast(dict[str, object], raw_payload)
            if payload.get("success") is False:
                return WebPageAttempt.failure(
                    adapter_id="firecrawl_reader",
                    adapter_version=self._adapter_version,
                    requested_url=url,
                    http_status=response.status_code,
                    error_category="provider_rejected",
                    retryable=False,
                    fallback_allowed=True,
                )
            data = payload.get("data")
            record = data if isinstance(data, dict) else payload
            markdown = record.get("markdown")
            if not isinstance(markdown, str):
                return WebPageAttempt.failure(
                    adapter_id="firecrawl_reader",
                    adapter_version=self._adapter_version,
                    requested_url=url,
                    http_status=response.status_code,
                    error_category="unusable_extraction",
                    retryable=False,
                    fallback_allowed=True,
                )
            content_text = markdown.strip()
            if len(content_text) < 80:
                return WebPageAttempt.failure(
                    adapter_id="firecrawl_reader",
                    adapter_version=self._adapter_version,
                    requested_url=url,
                    http_status=response.status_code,
                    error_category="unusable_extraction",
                    retryable=False,
                    fallback_allowed=True,
                )
            metadata = record.get("metadata")
            metadata_record = metadata if isinstance(metadata, dict) else {}
            final_url = (
                _optional_string(metadata_record.get("sourceURL"))
                or _optional_string(metadata_record.get("url"))
                or url
            )
            title = _optional_string(metadata_record.get("title")) or urlparse(final_url).netloc
            status_code = metadata_record.get("statusCode")
            return WebPageAttempt.success(
                adapter_id="firecrawl_reader",
                adapter_version=self._adapter_version,
                requested_url=url,
                final_url=final_url,
                http_status=(
                    int(status_code) if isinstance(status_code, int) else response.status_code
                ),
                content_type="text/markdown",
                title=title,
                content=content_text,
                complete=True,
                truncated=False,
            )
        except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError):
            return WebPageAttempt.failure(
                adapter_id="firecrawl_reader",
                adapter_version=self._adapter_version,
                requested_url=url,
                error_category="provider_unavailable",
                retryable=True,
                fallback_allowed=True,
            )


# Descriptive name matching the existing JinaReaderWebPageAdapter convention.
FirecrawlReaderWebPageAdapter = FirecrawlWebPageAdapter


class JinaReaderWebPageAdapter:
    """将 Jina Hosted Reader 响应规范化为正文获取尝试"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        adapter_version: str = "hosted-v1",
    ) -> None:
        """配置 Jina 凭证、HTTP 客户端和审计版本"""
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=False)
        self._adapter_version = adapter_version

    def fetch(self, url: str) -> WebPageAttempt:
        """读取公开 URL 并隔离 Jina wire response"""
        headers = {
            "Accept": "application/json",
            "X-Preset": "research",
            "X-Markdown-Chunking": "h3",
            "X-Retain-Images": "alt",
            "X-Retain-Media": "none",
            "X-Retain-Links": "text",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = self._client.get(f"https://r.jina.ai/{url}", headers=headers)
            if response.status_code >= 400:
                return WebPageAttempt.failure(
                    adapter_id="jina_reader",
                    adapter_version=self._adapter_version,
                    requested_url=url,
                    http_status=response.status_code,
                    content_type=response.headers.get("content-type"),
                    error_category=_jina_error_category(response),
                    retryable=(
                        response.status_code in {408, 409, 429}
                        or response.status_code >= 500
                    ),
                    fallback_allowed=_jina_fallback_allowed(response),
                )
            payload = cast(dict[str, object], response.json())
            data = payload.get("data")
            record = data if isinstance(data, dict) else payload
            content = data if isinstance(data, str) else record.get("content")
            content_text = str(content or "").strip()
            if len(content_text) < 80:
                return WebPageAttempt.failure(
                    adapter_id="jina_reader",
                    adapter_version=self._adapter_version,
                    requested_url=url,
                    http_status=response.status_code,
                    content_type=_optional_string(record.get("contentType")),
                    error_category="unusable_extraction",
                    retryable=False,
                    fallback_allowed=True,
                )
            final_url = _optional_string(record.get("url")) or url
            title = _optional_string(record.get("title")) or urlparse(final_url).netloc
            warning = record.get("warning") or record.get("warnings")
            truncated = bool(record.get("truncated", False))
            completeness = (_optional_string(record.get("completeness")) or "complete").casefold()
            return WebPageAttempt.success(
                adapter_id="jina_reader",
                adapter_version=self._adapter_version,
                requested_url=url,
                final_url=final_url,
                http_status=response.status_code,
                content_type=_optional_string(record.get("contentType")) or "text/markdown",
                title=title,
                content=content_text,
                complete=not truncated and completeness == "complete",
                truncated=truncated,
                warning_category="provider_warning" if warning else None,
            )
        except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError):
            return WebPageAttempt.failure(
                adapter_id="jina_reader",
                adapter_version=self._adapter_version,
                requested_url=url,
                error_category="provider_unavailable",
                retryable=True,
                fallback_allowed=True,
            )


class _VisibleTextExtractor(HTMLParser):
    """提取 HTML 中可阅读的文本内容"""

    def __init__(self) -> None:
        """初始化忽略标签与文本缓冲区"""
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """进入不可见标签时暂停收集文本"""
        del attrs
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        """离开不可见标签时恢复收集文本"""
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self._hidden_depth = max(0, self._hidden_depth - 1)

    def handle_data(self, data: str) -> None:
        """保存可见的非空文本片段"""
        normalized = " ".join(data.split())
        if self._hidden_depth == 0 and normalized:
            self._parts.append(normalized)

    def text(self) -> str:
        """返回合并后的可阅读正文"""
        return "\n".join(self._parts)


class HttpWebPageGateway:
    """受限 HTTP 网页正文抓取 Adapter"""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        max_response_bytes: int = 1_000_000,
        max_content_chars: int = 50_000,
    ) -> None:
        """设置超时客户端和正文上限"""
        self._client = client or httpx.Client(timeout=10.0, follow_redirects=False)
        self._max_response_bytes = max_response_bytes
        self._max_content_chars = max_content_chars

    def fetch(self, url: str) -> WebPageAttempt:
        """抓取公开文本网页并限制重定向与响应大小"""
        current_url = url
        try:
            for _ in range(4):
                _validate_public_url(current_url)
                with self._client.stream(
                    "GET",
                    current_url,
                    headers={
                        "Accept": "text/html,text/plain;q=0.9",
                        "User-Agent": "DeepResearcher/0.1",
                    },
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return WebPageAttempt.failure(
                                adapter_id="local_http",
                                adapter_version="stdlib-html-v1",
                                requested_url=url,
                                final_url=current_url,
                                http_status=response.status_code,
                                error_category="invalid_redirect",
                                retryable=False,
                                fallback_allowed=False,
                            )
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").casefold()
                    if "text/html" not in content_type and "text/plain" not in content_type:
                        return WebPageAttempt.failure(
                            adapter_id="local_http",
                            adapter_version="stdlib-html-v1",
                            requested_url=url,
                            final_url=current_url,
                            http_status=response.status_code,
                            content_type=content_type or None,
                            error_category="unsupported_content_type",
                            retryable=False,
                            fallback_allowed=False,
                        )
                    body = _read_limited_body(response, self._max_response_bytes)
                    raw_text = body.decode(response.encoding or "utf-8", errors="replace")
                    content = _extract_content(raw_text, content_type)
                    if len(content) < 80:
                        return WebPageAttempt.failure(
                            adapter_id="local_http",
                            adapter_version="stdlib-html-v1",
                            requested_url=url,
                            final_url=current_url,
                            http_status=response.status_code,
                            content_type=content_type,
                            error_category="unusable_extraction",
                            retryable=False,
                            fallback_allowed=False,
                        )
                    truncated = len(content) > self._max_content_chars
                    return WebPageAttempt.success(
                        adapter_id="local_http",
                        adapter_version="stdlib-html-v1",
                        requested_url=url,
                        final_url=current_url,
                        http_status=response.status_code,
                        content_type=content_type,
                        title=_extract_title(raw_text) or urlparse(current_url).netloc,
                        content=content[: self._max_content_chars],
                        complete=not truncated,
                        truncated=truncated,
                    )
        except ValueError:
            return WebPageAttempt.failure(
                adapter_id="local_http",
                adapter_version="stdlib-html-v1",
                requested_url=url,
                final_url=current_url,
                error_category="unsafe_url",
                retryable=False,
                fallback_allowed=False,
            )
        except (httpx.HTTPError, OSError):
            return WebPageAttempt.failure(
                adapter_id="local_http",
                adapter_version="stdlib-html-v1",
                requested_url=url,
                final_url=current_url,
                error_category="network_failure",
                retryable=True,
                fallback_allowed=False,
            )
        return WebPageAttempt.failure(
            adapter_id="local_http",
            adapter_version="stdlib-html-v1",
            requested_url=url,
            final_url=current_url,
            error_category="redirect_limit",
            retryable=False,
            fallback_allowed=False,
        )


def _normalize_attempt(
    result: WebPageAttempt | FetchedWebPage | None,
    *,
    adapter_id: str,
    adapter_version: str,
    requested_url: str,
) -> WebPageAttempt:
    """兼容旧测试 Adapter 并统一为获取尝试"""
    if isinstance(result, WebPageAttempt):
        return result
    if isinstance(result, dict) and result.get("content"):
        content = str(result["content"])
        truncated = bool(result.get("truncated", False))
        return WebPageAttempt.success(
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            requested_url=requested_url,
            final_url=requested_url,
            http_status=None,
            content_type=None,
            title=str(result.get("title") or urlparse(requested_url).netloc),
            content=content,
            complete=not truncated,
            truncated=truncated,
        )
    return WebPageAttempt.failure(
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        requested_url=requested_url,
        error_category="unusable_extraction",
        retryable=False,
        fallback_allowed=True,
    )


def _reject_unsafe_final_url(
    attempt: WebPageAttempt,
    validator: Callable[[str], None],
) -> WebPageAttempt:
    """拒绝 Adapter 成功后指向非公开地址的正文"""
    if attempt.status != "success" or attempt.final_url is None:
        return attempt
    try:
        validator(attempt.final_url)
    except ValueError:
        return WebPageAttempt.failure(
            adapter_id=attempt.adapter_id,
            adapter_version=attempt.adapter_version,
            requested_url=attempt.requested_url,
            final_url=attempt.final_url,
            http_status=attempt.http_status,
            content_type=attempt.content_type,
            error_category="unsafe_url",
            retryable=False,
            fallback_allowed=False,
            warning_category=attempt.warning_category,
        )
    return attempt


def _optional_string(value: object) -> str | None:
    """将 Provider 可选字段规范化为非空字符串"""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _jina_error_category(response: httpx.Response) -> str:
    """将 Jina 错误响应归一为稳定类别"""
    text = response.text.casefold()
    if "budgetexceeded" in text or response.status_code == 409:
        return "constraint_exhausted"
    if any(marker in text for marker in ("robots", "restricted", "authentication_required")):
        return "access_restricted"
    if response.status_code in {408, 429} or response.status_code >= 500:
        return "provider_unavailable"
    return "provider_rejected"


def _jina_fallback_allowed(response: httpx.Response) -> bool:
    """判断 Jina 失败后是否仍允许安全本地读取"""
    return _jina_error_category(response) not in {"constraint_exhausted", "access_restricted"}


def _firecrawl_error_category(response: httpx.Response) -> str:
    """将 Firecrawl 错误响应归一为稳定类别。"""
    if response.status_code == 402:
        return "constraint_exhausted"
    if response.status_code in {401, 403}:
        return "access_restricted"
    if response.status_code in {408, 429} or response.status_code >= 500:
        return "provider_unavailable"
    return "provider_rejected"


def _validate_public_url(url: str) -> None:
    """拒绝非 HTTP、私网和回环网页地址"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("网页地址无效")
    try:
        literal_address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None and not literal_address.is_global:
        raise ValueError("网页地址不允许访问私网")
    default_port = 443 if parsed.scheme == "https" else 80
    resolved_addresses = [
        ipaddress.ip_address(candidate[4][0])
        for candidate in socket.getaddrinfo(
        parsed.hostname, parsed.port or default_port, type=socket.SOCK_STREAM
        )
    ]
    if all(address.is_global for address in resolved_addresses):
        return
    if resolved_addresses and all(address in _FAKE_IP_NETWORK for address in resolved_addresses):
        _validate_fake_ip_hostname(parsed.hostname)
        return
    raise ValueError("网页地址不允许访问私网")


@lru_cache(maxsize=256)
def _validate_fake_ip_hostname(hostname: str) -> None:
    """通过公共 DNS 校验 Fake IP 背后的真实地址"""
    normalized = hostname.casefold().rstrip(".")
    if normalized == "localhost" or normalized.endswith((".localhost", ".local", ".internal")):
        raise ValueError("网页地址不允许访问私网")
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        for record_type in ("A", "AAAA"):
            response = httpx.get(
                "https://dns.google/resolve",
                params={"name": hostname, "type": record_type},
                timeout=5.0,
            )
            response.raise_for_status()
            for answer in response.json().get("Answer", []):
                try:
                    addresses.append(ipaddress.ip_address(str(answer.get("data", ""))))
                except ValueError:
                    continue
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("网页地址无法完成公共 DNS 校验") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("网页地址不允许访问私网")


def _read_limited_body(response: httpx.Response, maximum: int) -> bytes:
    """读取不超过给定字节数的 HTTP 响应正文"""
    parts: list[bytes] = []
    received = 0
    for part in response.iter_bytes():
        received += len(part)
        if received > maximum:
            raise ValueError("网页响应超过大小限制")
        parts.append(part)
    return b"".join(parts)


def _extract_content(raw_text: str, content_type: str) -> str:
    """从 HTML 或纯文本中提取规范化正文"""
    if "text/html" not in content_type:
        return "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
    extractor = _VisibleTextExtractor()
    extractor.feed(raw_text)
    return extractor.text()


def _extract_title(raw_html: str) -> str | None:
    """从 HTML title 标签读取页面标题"""
    start = raw_html.casefold().find("<title")
    if start < 0:
        return None
    opening_end = raw_html.find(">", start)
    closing = raw_html.casefold().find("</title>", opening_end + 1)
    if opening_end < 0 or closing < 0:
        return None
    title = " ".join(raw_html[opening_end + 1 : closing].split())
    return title[:1000] or None
