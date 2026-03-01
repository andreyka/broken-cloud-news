from __future__ import annotations

import socket

import pytest

from bcn.common.url_policy import assert_public_http_url
from bcn.common.url_policy import trusted_hosts_from_urls
from bcn.common.url_policy import URLValidationError


def _ai(ip: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]


def test_blocks_localhost():
    with pytest.raises(URLValidationError):
        assert_public_http_url("http://localhost:8080/path")


def test_blocks_private_ip_literal():
    with pytest.raises(URLValidationError):
        assert_public_http_url("http://10.0.0.12/data")


def test_allows_trusted_private_host():
    assert_public_http_url(
        "http://192.168.0.9:8188/view?filename=cover.png",
        trusted_hosts={"192.168.0.9"},
    )


def test_blocks_hostname_resolving_to_private_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _ai("10.2.3.4"))
    with pytest.raises(URLValidationError):
        assert_public_http_url("https://news.example.org/story")


def test_allows_hostname_resolving_to_public_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _ai("93.184.216.34"))
    assert_public_http_url("https://news.example.org/story")


def test_trusted_hosts_from_urls_extracts_hostname():
    assert trusted_hosts_from_urls(
        ["http://192.168.0.9:8188", "https://example.com/path"]) == {
            "192.168.0.9",
            "example.com",
        }
