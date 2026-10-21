"""URL canonicalization and SSRF protection for AOI."""

import hashlib
import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
        "source",
        "_ga",
        "sessionid",
        "jsessionid",
        "phpsessid",
    }
)

_MULTISLASH = re.compile(r"/{2,}")

# RFC 6052 well-known NAT64 prefix: the last 32 bits are an IPv4 address.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def _is_public(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses.

    The stdlib's ``is_global`` already resolves IPv4-mapped and Teredo forms.
    6to4 and NAT64 addresses are judged by the IPv4 address they wrap.
    """
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.sixtofour is not None:
            return _is_public(addr.sixtofour)
        if addr in _NAT64_PREFIX:
            return _is_public(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    return addr.is_global and not addr.is_multicast


class UrlTooLong(ValueError):
    """Raised when the canonical URL exceeds 2048 bytes."""


class BlockedAddress(Exception):
    """Raised when a hostname resolves to a private or reserved IP address."""


class ResolutionFailed(BlockedAddress):
    """Raised when a hostname does not resolve; a BlockedAddress, so callers still fail closed."""


def canonicalize(
    url: str,
    *,
    drop_params: set[str] | None = None,
    keep_only: set[str] | None = None,
    keep_fragment: bool = False,
    keep_trailing_slash: bool = False,
) -> str:
    """Reduce a URL to one canonical spelling. Idempotent by construction.

    Steps applied:
    - Strip leading/trailing whitespace
    - Lowercase scheme and host
    - Strip default port (80 for http, 443 for https)
    - Normalize path: collapse multiple slashes, strip trailing slash (unless keep_trailing_slash)
    - Filter query params: remove tracking + drop_params, or keep only keep_only; sort remainder
    - Strip fragment unless keep_fragment=True or fragment starts with '!' (hashbang)

    Raises UrlTooLong if result > 2048 bytes.
    """
    url = url.strip()
    parts = urlsplit(url)

    # Lowercase scheme and host
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()

    # Strip default port
    if ":" in host:
        hostname, port_str = host.rsplit(":", 1)
        if (scheme == "http" and port_str == "80") or (scheme == "https" and port_str == "443"):
            host = hostname

    # Normalize path: collapse multiple slashes
    path = _MULTISLASH.sub("/", parts.path)

    # Strip trailing slash (unless keep_trailing_slash or path is root "/")
    if not keep_trailing_slash and path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Filter query params
    raw_pairs = parse_qsl(parts.query, keep_blank_values=True)
    if keep_only is not None:
        pairs = [(k, v) for k, v in raw_pairs if k in keep_only]
    else:
        blocked = TRACKING_PARAMS | (drop_params or set())
        pairs = [(k, v) for k, v in raw_pairs if k not in blocked]

    # Sort pairs for determinism
    pairs.sort()
    query = urlencode(pairs)

    # Handle fragment
    fragment = ""
    if parts.fragment and (keep_fragment or parts.fragment.startswith("!")):
        fragment = parts.fragment

    result = urlunsplit((scheme, host, path, query, fragment))

    if len(result.encode()) > 2048:
        raise UrlTooLong(f"Canonical URL exceeds 2048 bytes: {len(result.encode())} bytes")

    return result


def url_hash(canonical_url: str) -> bytes:
    """Return 32 raw bytes (SHA-256) of the canonical URL, for the unique index on crawl_url."""
    return hashlib.sha256(canonical_url.encode()).digest()


def validate_host(host: str) -> str:
    """Resolve hostname, check every resolved address, return the first one.

    Raises BlockedAddress if any resolved address is not globally routable
    unicast, or cannot be parsed (fail closed).
    """
    try:
        results = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ResolutionFailed(f"DNS resolution failed for {host!r}: {exc}") from exc

    if not results:
        raise ResolutionFailed(f"No addresses resolved for {host!r}")

    for _family, _type, _proto, _canonname, sockaddr in results:
        ip_str = str(sockaddr[0])
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise BlockedAddress(f"Host {host!r} resolved to unparseable {ip_str!r}") from exc

        if not _is_public(addr):
            raise BlockedAddress(f"Host {host!r} resolved to non-public address {ip_str}")

    return str(results[0][4][0])
