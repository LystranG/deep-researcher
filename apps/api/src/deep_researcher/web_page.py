import ipaddress
import socket
from functools import lru_cache
from html.parser import HTMLParser
from typing import Protocol, TypedDict
from urllib.parse import urljoin, urlparse

import httpx

_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class FetchedWebPage(TypedDict):
    """受限网页抓取后可持久化的正文"""

    title: str
    content: str
    truncated: bool


class WebPageGateway(Protocol):
    """读取单个公开网页正文的受限 Adapter"""

    def fetch(self, url: str) -> FetchedWebPage | None: ...


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

    def fetch(self, url: str) -> FetchedWebPage | None:
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
                            return None
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").casefold()
                    if "text/html" not in content_type and "text/plain" not in content_type:
                        return None
                    body = _read_limited_body(response, self._max_response_bytes)
                    raw_text = body.decode(response.encoding or "utf-8", errors="replace")
                    content = _extract_content(raw_text, content_type)
                    if len(content) < 80:
                        return None
                    truncated = len(content) > self._max_content_chars
                    return {
                        "title": _extract_title(raw_text) or urlparse(current_url).netloc,
                        "content": content[: self._max_content_chars],
                        "truncated": truncated,
                    }
        except (httpx.HTTPError, OSError, ValueError):
            return None
        return None


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
