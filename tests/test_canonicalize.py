"""Tests for URL canonicalization and SSRF protection — Stage 4."""

import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_url(path: str = "/path") -> str:
    return f"https://example.com{path}"


# ---------------------------------------------------------------------------
# 1. Known normalization cases (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Strip tracking params
        (
            "https://example.com/job?utm_source=email&id=42",
            "https://example.com/job?id=42",
        ),
        # Lowercase scheme + host
        (
            "HTTPS://EXAMPLE.COM/Job",
            "https://example.com/Job",
        ),
        # Strip trailing slash from path
        (
            "https://example.com/jobs/",
            "https://example.com/jobs",
        ),
        # Sort query params
        (
            "https://example.com/search?z=1&a=2",
            "https://example.com/search?a=2&z=1",
        ),
        # Strip fragment
        (
            "https://example.com/page#section",
            "https://example.com/page",
        ),
    ],
)
def test_canonicalize_known_cases(raw: str, expected: str) -> None:
    """canonicalize reduces known URL variants to their expected canonical form."""
    from aoi.pipeline.canonicalize import canonicalize

    assert canonicalize(raw) == expected


# ---------------------------------------------------------------------------
# 2. Multiple tracking params all removed
# ---------------------------------------------------------------------------


def test_canonicalize_removes_all_tracking_params() -> None:
    """Two URLs differing only by tracking params produce the same canonical form."""
    from aoi.pipeline.canonicalize import canonicalize

    base = "https://example.com/job?id=42"
    with_tracking = (
        "https://example.com/job?id=42&utm_source=email&utm_medium=cpc"
        "&utm_campaign=spring&gclid=abc123&fbclid=xyz"
    )
    assert canonicalize(base) == canonicalize(with_tracking)


# ---------------------------------------------------------------------------
# 3. Hypothesis: idempotence
# ---------------------------------------------------------------------------


@given(
    st.from_regex(
        r"https://[a-z]{3,10}\.[a-z]{2,4}/[a-z0-9/]{0,20}(\?[a-z]{1,5}=[a-z0-9]{1,5})?",
        fullmatch=True,
    )
)
@h_settings(max_examples=200)
def test_canonicalize_is_idempotent(url: str) -> None:
    """canonicalize(canonicalize(url)) == canonicalize(url) for any valid-looking URL."""
    from aoi.pipeline.canonicalize import canonicalize

    first = canonicalize(url)
    second = canonicalize(first)
    assert first == second


# ---------------------------------------------------------------------------
# 4. UrlTooLong raised for excessively long URLs
# ---------------------------------------------------------------------------


def test_canonicalize_raises_url_too_long() -> None:
    """UrlTooLong is raised when canonical URL exceeds 2048 bytes."""
    from aoi.pipeline.canonicalize import UrlTooLong, canonicalize

    long_url = "https://example.com/" + "a" * 2100
    with pytest.raises(UrlTooLong):
        canonicalize(long_url)


# ---------------------------------------------------------------------------
# 5. url_hash returns 32 bytes
# ---------------------------------------------------------------------------


def test_url_hash_returns_32_bytes() -> None:
    """url_hash returns exactly 32 bytes (SHA-256)."""
    from aoi.pipeline.canonicalize import url_hash

    result = url_hash("https://example.com/job?id=42")
    assert isinstance(result, bytes)
    assert len(result) == 32


# ---------------------------------------------------------------------------
# 6. url_hash is deterministic
# ---------------------------------------------------------------------------


def test_url_hash_is_deterministic() -> None:
    """url_hash returns the same bytes for the same input."""
    from aoi.pipeline.canonicalize import url_hash

    h1 = url_hash("https://example.com/job")
    h2 = url_hash("https://example.com/job")
    assert h1 == h2


# ---------------------------------------------------------------------------
# 7. SSRF: private/reserved IPs are blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host, ip",
    [
        ("internal.corp", "10.0.0.1"),
        ("local.corp", "192.168.1.1"),
        ("link-local.corp", "169.254.169.254"),
        ("loopback.corp", "127.0.0.1"),
        ("zero-net.corp", "0.0.0.1"),
        ("cgnat.corp", "100.64.0.1"),
        ("rfc1918-172.corp", "172.16.0.1"),
        ("benchmark.corp", "198.18.0.1"),
        ("multicast.corp", "224.0.0.1"),
        ("reserved.corp", "240.0.0.1"),
        ("broadcast.corp", "255.255.255.255"),
        ("ietf-protocol.corp", "192.0.0.1"),
        ("test-net.corp", "192.0.2.1"),
    ],
)
def test_validate_host_blocks_private_ips(
    host: str,
    ip: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate_host raises BlockedAddress for hosts that resolve to private IPs."""
    import socket

    from aoi.pipeline.canonicalize import BlockedAddress, validate_host

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))],
    )
    with pytest.raises(BlockedAddress):
        validate_host(host)


# ---------------------------------------------------------------------------
# 8. SSRF: cloud metadata endpoint blocked
# ---------------------------------------------------------------------------


def test_validate_host_blocks_metadata_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_host blocks the AWS/GCP metadata IP 169.254.169.254."""
    import socket

    from aoi.pipeline.canonicalize import BlockedAddress, validate_host

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 80))],
    )
    with pytest.raises(BlockedAddress):
        validate_host("metadata.google.internal")


# ---------------------------------------------------------------------------
# 9. SSRF: public IPs are allowed
# ---------------------------------------------------------------------------


def test_validate_host_allows_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_host returns the resolved IP for public addresses."""
    import socket

    from aoi.pipeline.canonicalize import validate_host

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))],
    )
    result = validate_host("example.com")
    assert result == "93.184.216.34"


# ---------------------------------------------------------------------------
# 10. keep_fragment=True preserves fragment
# ---------------------------------------------------------------------------


def test_canonicalize_keep_fragment() -> None:
    """keep_fragment=True preserves the URL fragment."""
    from aoi.pipeline.canonicalize import canonicalize

    url = "https://example.com/page#section"
    result = canonicalize(url, keep_fragment=True)
    assert result == "https://example.com/page#section"


# ---------------------------------------------------------------------------
# 11. keep_only filters to specified params only
# ---------------------------------------------------------------------------


def test_canonicalize_keep_only() -> None:
    """keep_only retains only the specified query parameters."""
    from aoi.pipeline.canonicalize import canonicalize

    url = "https://example.com/search?a=1&b=2&c=3"
    result = canonicalize(url, keep_only={"a", "c"})
    assert "b=" not in result
    assert "a=1" in result
    assert "c=3" in result


# ---------------------------------------------------------------------------
# 12. drop_params removes specified params
# ---------------------------------------------------------------------------


def test_canonicalize_drop_params() -> None:
    """drop_params removes the specified query parameters."""
    from aoi.pipeline.canonicalize import canonicalize

    url = "https://example.com/search?a=1&secret=xyz&c=3"
    result = canonicalize(url, drop_params={"secret"})
    assert "secret" not in result
    assert "a=1" in result


# ---------------------------------------------------------------------------
# 13. keep_trailing_slash=True preserves trailing slash
# ---------------------------------------------------------------------------


def test_canonicalize_keep_trailing_slash() -> None:
    """keep_trailing_slash=True preserves the trailing slash."""
    from aoi.pipeline.canonicalize import canonicalize

    url = "https://example.com/jobs/"
    result = canonicalize(url, keep_trailing_slash=True)
    assert result.endswith("/")


# ---------------------------------------------------------------------------
# 14. Hashbang fragments (#!) are preserved even without keep_fragment
# ---------------------------------------------------------------------------


def test_canonicalize_keeps_hashbang_fragment() -> None:
    """Hashbang (#!) fragments are kept even without keep_fragment=True."""
    from aoi.pipeline.canonicalize import canonicalize

    url = "https://example.com/app#!/jobs/42"
    result = canonicalize(url)
    assert "#!/jobs/42" in result


# ---------------------------------------------------------------------------
# 15. Default port is stripped (http:80, https:443)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url_with_port, expected_no_port",
    [
        ("http://example.com:80/path", "http://example.com/path"),
        ("https://example.com:443/path", "https://example.com/path"),
    ],
)
def test_canonicalize_strips_default_port(url_with_port: str, expected_no_port: str) -> None:
    """canonicalize removes default ports (:80 for http, :443 for https)."""
    from aoi.pipeline.canonicalize import canonicalize

    assert canonicalize(url_with_port) == expected_no_port


# ---------------------------------------------------------------------------
# 16. DNS resolution failure raises BlockedAddress
# ---------------------------------------------------------------------------


def test_validate_host_dns_failure_raises_blocked_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate_host raises BlockedAddress when DNS resolution fails."""
    import socket

    from aoi.pipeline.canonicalize import BlockedAddress, validate_host

    def failing_getaddrinfo(*args: object, **kwargs: object) -> list[object]:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", failing_getaddrinfo)

    with pytest.raises(BlockedAddress):
        validate_host("nonexistent.invalid")


# ---------------------------------------------------------------------------
# 17. Empty DNS results raises BlockedAddress
# ---------------------------------------------------------------------------


def test_validate_host_empty_results_raises_blocked_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate_host raises BlockedAddress when getaddrinfo returns empty list."""
    import socket

    from aoi.pipeline.canonicalize import BlockedAddress, validate_host

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [])

    with pytest.raises(BlockedAddress):
        validate_host("empty.example")


@pytest.mark.parametrize("failure", ["gaierror", "empty"])
def test_validate_host_reports_a_failed_lookup_as_resolution_failed(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A lookup that fails is told apart from a blocked address, and still fails closed."""
    import socket

    from aoi.pipeline.canonicalize import BlockedAddress, ResolutionFailed, validate_host

    def getaddrinfo(*args: object, **kwargs: object) -> list[object]:
        if failure == "gaierror":
            raise socket.gaierror("Name or service not known")
        return []

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    with pytest.raises(ResolutionFailed) as caught:
        validate_host("unresolvable.example")
    assert isinstance(caught.value, BlockedAddress)


# ---------------------------------------------------------------------------
# IPv6-mapped IPv4 bypass prevention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mapped_ip",
    [
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:192.168.1.1",
        "::ffff:169.254.169.254",
    ],
)
def test_validate_host_blocks_ipv6_mapped_ipv4(
    mapped_ip: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate_host blocks IPv6-mapped IPv4 addresses that map to private ranges."""
    import socket

    from aoi.pipeline.canonicalize import BlockedAddress, validate_host

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", (mapped_ip, 0, 0, 0))],
    )
    with pytest.raises(BlockedAddress):
        validate_host("evil.example")


# ---------------------------------------------------------------------------
# IPv6: non-global ranges and IPv4 addresses embedded in IPv6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ipv6",
    [
        "::1",  # loopback
        "::",  # unspecified
        "fe80::1",  # link-local
        "fc00::1",  # unique local
        "2002:a00:1::",  # 6to4 wrapping 10.0.0.1
        "64:ff9b::a00:1",  # NAT64 well-known prefix wrapping 10.0.0.1
        "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # Teredo, client 10.0.0.1
    ],
)
def test_validate_host_blocks_non_global_ipv6(
    ipv6: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate_host blocks non-global IPv6 and IPv6 forms that embed a private IPv4."""
    import socket

    from aoi.pipeline.canonicalize import BlockedAddress, validate_host

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", (ipv6, 0, 0, 0))],
    )
    with pytest.raises(BlockedAddress):
        validate_host("evil.example")


@pytest.mark.parametrize(
    "ipv6",
    [
        "2606:4700:4700::1111",  # public
        "2002:5db8:d822::",  # 6to4 wrapping public 93.184.216.34
    ],
)
def test_validate_host_allows_global_ipv6(
    ipv6: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate_host allows global IPv6, including 6to4 that wraps a public IPv4."""
    import socket

    from aoi.pipeline.canonicalize import validate_host

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", (ipv6, 0, 0, 0))],
    )
    assert validate_host("public.example") == ipv6


def test_validate_host_blocks_unparseable_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_host fails closed when the resolver returns something that is not an IP."""
    import socket

    from aoi.pipeline.canonicalize import BlockedAddress, validate_host

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("not-an-ip", 0))],
    )
    with pytest.raises(BlockedAddress):
        validate_host("weird.example")
