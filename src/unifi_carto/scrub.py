"""Deterministic scrubber that turns real API captures into publishable test fixtures.

Pure: the output depends only on the input and the salt, so separate files scrubbed
with the same salt stay joinable (a MAC, IP, UUID or name maps identically everywhere).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import uuid
from typing import Any, Final

# Substrings that mark a key as secret or uniquely identifying hardware.
_DROP_SUBSTRINGS: Final = ("pass", "secret", "psk", "token", "serial", "license")
# Descriptive, enum-like keys kept verbatim (a firmware version like 10.6.106.1 must not be read as an IP).
_KEEP_SUBSTRINGS: Final = ("version", "model", "type", "media", "shortname", "state", "status", "connector")
# Identity keys (compared lowercase with "_" removed) whose string values are replaced by ``<kind>-<hex>``.
_IDENTITY_KEYS: Final = {
    "name": "name",
    "hostname": "host",
    "displayname": "name",
    "devicename": "device",
    "ssid": "ssid",
    "essid": "ssid",
    "sitename": "site",
    "desc": "site",
    "label": "label",
    "username": "user",
    "email": "user",
    "fullname": "user",
    "note": "note",
}

_MAC_RE: Final = re.compile(
    r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}([:-]))(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])"
)
_UUID_RE: Final = re.compile(
    r"(?<![0-9A-Fa-f-])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f-])"
)
_IPV4_RE: Final = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_KEY_WORDS: Final = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")

_RFC1918: Final = (
    (ipaddress.IPv4Network("10.0.0.0/8"), 16),
    (ipaddress.IPv4Network("172.16.0.0/12"), 12),
    (ipaddress.IPv4Network("192.168.0.0/16"), 8),
)
_PUBLIC_POOLS: Final = (ipaddress.IPv4Network("203.0.113.0/24"), ipaddress.IPv4Network("198.51.100.0/24"))


def scrub(obj: Any, *, salt: str) -> Any:
    """Return a scrubbed deep copy of ``obj`` (JSON-like data). See the module docstring."""
    return _Scrubber(salt.encode()).walk(obj, None)


def drop_key(key: str) -> bool:
    """True if a key names a secret or hardware identity that must not appear in fixtures.

    ``key`` is matched as a word (``apiKey``, ``x_iapp_key``, ``wpakey``) so that ``keyword``,
    ``keyboard`` or ``keepalive`` are kept; every legacy ``x_*`` field is a hidden secret.
    """
    lower = key.lower()
    if lower.startswith("x_") or any(s in lower for s in _DROP_SUBSTRINGS):
        return True
    return any(w.endswith(("key", "keys")) for w in (m.lower() for m in _KEY_WORDS.findall(key)))


class _Scrubber:
    def __init__(self, salt: bytes) -> None:
        self.salt = salt

    def digest(self, kind: str, value: str) -> bytes:
        return hmac.new(self.salt, f"{kind}\0{value}".encode(), hashlib.sha256).digest()

    def walk(self, obj: Any, key: str | None) -> Any:
        if isinstance(obj, dict):
            return {self.text(str(k)): self.walk(v, str(k)) for k, v in obj.items() if not drop_key(str(k))}
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            if key and any(s in key.lower() for s in _KEEP_SUBSTRINGS):
                return obj
            kind = _IDENTITY_KEYS.get(key.lower().replace("_", "")) if key else None
            if kind and obj:
                return f"{kind}-{self.digest('name', obj).hex()[:6]}"
            return self.text(obj)
        return obj

    # -- strings ---------------------------------------------------------------------------

    def text(self, value: str) -> str:
        whole = self.whole_ip(value)
        if whole is not None:
            return whole
        value = _UUID_RE.sub(lambda m: self.uuid(m.group(0)), value)
        value = _MAC_RE.sub(lambda m: self.mac(m.group(0)), value)
        return _IPV4_RE.sub(lambda m: self.ipv4_text(m.group(0)), value)

    def uuid(self, value: str) -> str:
        return str(uuid.UUID(bytes=self.digest("uuid", value.lower())[:16], version=5))

    def mac(self, value: str) -> str:
        norm = value.lower().replace("-", ":")
        tail = self.digest("mac", norm)[:5]
        return "02:" + ":".join(f"{b:02x}" for b in tail)

    def ipv4_text(self, value: str) -> str:
        try:
            return str(self.ipv4(ipaddress.IPv4Address(value)))
        except ValueError:
            return value  # not an address (e.g. a version string like 1.2.3.400)

    def whole_ip(self, value: str) -> str | None:
        """Scrub a string that is exactly an IPv6 address or an address/prefix; None if it is not."""
        if ":" not in value and "/" not in value:
            return None
        try:
            iface = ipaddress.ip_interface(value)
        except ValueError:
            return None
        if "/" not in value:
            return str(self.ip(iface.ip))
        mapped = self.ip(iface.ip)
        if iface.ip == iface.network.network_address:  # a subnet, not an address on one
            return str(ipaddress.ip_network(f"{mapped}/{iface.network.prefixlen}", strict=False))
        return f"{mapped}/{iface.network.prefixlen}"

    # -- addresses -------------------------------------------------------------------------

    def ip(self, addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        if isinstance(addr, ipaddress.IPv4Address):
            return self.ipv4(addr)
        return self.ipv6(addr)

    def ipv4(self, addr: ipaddress.IPv4Address) -> ipaddress.IPv4Address:
        if _keep_v4(addr):
            return addr
        packed = int(addr)
        host = packed & 0xFF
        for net, bits in _RFC1918:
            if addr in net:
                # Bijective keyed permutation of the /24 index inside the same RFC 1918 block:
                # distinct /24s stay distinct and the host octet is preserved.
                index = (packed - int(net.network_address)) >> 8
                return ipaddress.IPv4Address(int(net.network_address) + (self.permute(index, bits, net) << 8) + host)
        h = self.digest("ipv4", str(addr))
        pool = _PUBLIC_POOLS[h[0] & 1]
        return ipaddress.IPv4Address(int(pool.network_address) + 1 + h[1] % 254)

    def permute(self, value: int, bits: int, net: ipaddress.IPv4Network) -> int:
        half = bits // 2
        mask = (1 << half) - 1
        left, right = value >> half, value & mask
        for rnd in range(4):
            f = int.from_bytes(self.digest(f"feistel{net}{rnd}", str(right))[:4], "big") & mask
            left, right = right, left ^ f
        return (left << half) | right

    def ipv6(self, addr: ipaddress.IPv6Address) -> ipaddress.IPv6Address:
        if addr.is_unspecified or addr.is_loopback or addr.is_multicast:
            return addr
        if addr.ipv4_mapped is not None:
            return ipaddress.IPv6Address(f"::ffff:{self.ipv4(addr.ipv4_mapped)}")
        packed = int(addr)
        prefix = self.digest("ipv6net", str(packed >> 64))[:7]  # the /64 maps as a unit
        iid = self.digest("ipv6iid", str(addr))[:8]
        return ipaddress.IPv6Address(bytes([0xFD]) + prefix + iid)


def _keep_v4(addr: ipaddress.IPv4Address) -> bool:
    """Addresses that identify nothing: netmasks, loopback, link-local, multicast, broadcast, unspecified."""
    if addr.is_unspecified or addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return True
    bits = int(addr)
    inverted = ~bits & 0xFFFFFFFF
    return bits != 0 and (inverted & (inverted + 1)) == 0  # contiguous ones: a netmask or 255.255.255.255
