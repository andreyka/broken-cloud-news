"""URL validation helpers for SSRF hardening."""

from __future__ import annotations

from collections.abc import Iterable
import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_HOST_EXACT = frozenset({
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
})
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
)
_BLOCKED_METADATA_NETS = (
    ipaddress.ip_network("169.254.169.254/32"),
    ipaddress.ip_network("169.254.170.2/32"),
    ipaddress.ip_network("100.100.100.200/32"),
)


class URLValidationError(ValueError):
    """Raised when a URL fails SSRF policy checks."""


def normalize_hostname(value: str) -> str:
    """Normalize hostname for comparisons."""
    return (value or "").strip().rstrip(".").lower()


def host_from_url(url: str) -> str:
    """Extract normalized hostname from URL or return empty string."""
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return ""
    return normalize_hostname(parsed.hostname or "")


def trusted_hosts_from_urls(urls: Iterable[str] | None) -> set[str]:
    """Build trusted host set from URL list."""
    out: set[str] = set()
    for value in urls or []:
        host = host_from_url(str(value))
        if host:
            out.add(host)
    return out


def normalize_trusted_hosts(hosts: Iterable[str] | None) -> set[str]:
    """Normalize explicit trusted host entries."""
    out: set[str] = set()
    for value in hosts or []:
        text = str(value or "").strip()
        if not text:
            continue
        host = host_from_url(text)
        if not host:
            host = normalize_hostname(text)
        if host:
            out.add(host)
    return out


def assert_public_http_url(
    url: str,
    *,
    trusted_hosts: Iterable[str] | None = None,
) -> None:
    """Enforce URL policy for outbound fetches.

    Rules:
    - only ``http``/``https`` schemes
    - reject local/internal hostnames
    - reject private/reserved/link-local/loopback IPs
    - resolve DNS and reject hostnames resolving to non-public IPs
    - trusted hosts bypass private-IP restrictions (for known internal services)
    """
    raw = (url or "").strip()
    if not raw:
        raise URLValidationError("empty URL")

    try:
        parsed = urlparse(raw)
    except Exception as exc:
        raise URLValidationError("invalid URL") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise URLValidationError(f"unsupported URL scheme: {scheme or 'none'}")

    host = normalize_hostname(parsed.hostname or "")
    if not host:
        raise URLValidationError("URL has no hostname")

    trusted = normalize_trusted_hosts(trusted_hosts)
    if host in trusted:
        return

    if host in _BLOCKED_HOST_EXACT or any(
            host.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES):
        raise URLValidationError(f"blocked local/internal hostname: {host}")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if not _is_public_ip(ip):
            raise URLValidationError(f"blocked non-public IP: {ip}")
        return

    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise URLValidationError(f"hostname resolution failed for {host}") from exc

    if not infos:
        raise URLValidationError(f"hostname resolution failed for {host}")

    for info in infos:
        addr = info[4][0]
        ip_txt = addr.split("%", 1)[0]  # strip IPv6 zone id if present
        try:
            resolved_ip = ipaddress.ip_address(ip_txt)
        except ValueError as exc:
            raise URLValidationError(
                f"hostname resolved to invalid address: {ip_txt}") from exc
        if not _is_public_ip(resolved_ip):
            raise URLValidationError(
                f"hostname resolves to non-public IP: {resolved_ip}")


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if any(ip in net for net in _BLOCKED_METADATA_NETS):
        return False
    return bool(ip.is_global)
