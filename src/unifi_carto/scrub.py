"""Deterministic scrubber that turns real API captures into publishable test fixtures.

The output depends only on the input and the salt, so separate files scrubbed with the same
salt stay joinable (a MAC, IP, UUID or name maps identically everywhere). The one exception is
public IPv4: it maps through a per-salt table that lives for the process (one capture run), so
the mapping is injective; collisions are resolved in order of first appearance.
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
# Location keys, matched on the first word of the key (``geo_info``, ``latitude``, ``ispName``).
# ``lat``/``lon`` are whole words so that ``latency`` and ``long_retry`` are kept.
_LOCATION_PREFIXES: Final = ("geo", "country", "isp", "latitude", "longitude")
_LOCATION_WORDS: Final = frozenset({"lat", "lon", "lng"})
_LOCATION_KEYS: Final = frozenset({"city", "address", "timezone"})
# Descriptive, enum-like keys kept verbatim (a firmware version like 10.6.106.1 must not be read as an IP,
# an interface name like eth8 is not an identity).
_KEEP_SUBSTRINGS: Final = (
    "version",
    "model",
    "type",
    "media",
    "shortname",
    "state",
    "status",
    "connector",
    "purpose",
    "ifname",
    "radio",
)
# Identity keys by word: exact words, and word prefixes (``port_descr``, ``domain_name``, ``notes``).
_IDENTITY_WORDS: Final = {"alias": "name", "label": "label", "ssid": "ssid", "essid": "ssid", "email": "user"}
_IDENTITY_PREFIXES: Final = (("desc", "desc"), ("domain", "domain"), ("note", "note"), ("comment", "note"))
# Identity keys (compared lowercase with "_" removed) with a specific kind; any other key ending in
# ``name`` is an identity of kind ``name``. String values become ``<kind>-<hex>``.
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

_HEX: Final = "[0-9A-Fa-f]"
# One pass over a string; alternatives are tried in order at each position, so a UUID wins over the
# bare MAC that is its last group, and an IPv6 address over the MAC-like groups inside it.
_TOKEN_RE: Final = re.compile(
    rf"""
    (?P<uuid>(?<![0-9A-Fa-f-]){_HEX}{{8}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{12}}(?![0-9A-Fa-f-]))
    # IPv6 (optionally with an embedded IPv4 tail or a prefix length): must contain "::" or have 8 groups.
    | (?P<ipv6>(?<![0-9A-Fa-f:.])
        (?=[0-9A-Fa-f:]*::|(?:{_HEX}{{1,4}}:){{7}}{_HEX}{{1,4}}(?![0-9A-Fa-f:]))
        [0-9A-Fa-f:]*[0-9A-Fa-f:](?:(?<=:)\d{{1,3}}(?:\.\d{{1,3}}){{3}})?(?:/\d{{1,3}})?
        (?![0-9A-Fa-f:.]))
    # MAC, colon or dash form: not inside a longer run of hex groups, except after a "mac:" prefix.
    | (?P<mac>(?:(?<![0-9A-Fa-f])(?<![0-9A-Fa-f][:-])|(?<=[Mm][Aa][Cc][:-]))
        {_HEX}{{2}}(?P<sep>[:-])(?:{_HEX}{{2}}(?P=sep)){{4}}{_HEX}{{2}}(?![0-9A-Fa-f])(?![:-]{_HEX}))
    | (?P<macdot>(?<![0-9A-Fa-f.]){_HEX}{{4}}\.{_HEX}{{4}}\.{_HEX}{{4}}(?![0-9A-Fa-f.]))
    | (?P<macbare>(?<![0-9A-Fa-f]){_HEX}{{12}}(?![0-9A-Fa-f]))
    | (?P<ipv4>(?<![\d.])(?:\d{{1,3}}\.){{3}}\d{{1,3}}(?![\d.]))
    """,
    re.VERBOSE,
)
_KEY_WORDS: Final = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")

_RFC1918: Final = (
    (ipaddress.IPv4Network("10.0.0.0/8"), 16),
    (ipaddress.IPv4Network("172.16.0.0/12"), 12),
    (ipaddress.IPv4Network("192.168.0.0/16"), 8),
)
# Public IPv4 maps to TEST-NET-3/TEST-NET-2 hosts, then (past 508 distinct addresses) to benchmark space.
_PUBLIC_SLOTS: Final = tuple(
    int(net.network_address) + host
    for net in (ipaddress.IPv4Network("203.0.113.0/24"), ipaddress.IPv4Network("198.51.100.0/24"))
    for host in range(1, 255)
)
_OVERFLOW: Final = ipaddress.IPv4Network("198.18.0.0/15")
# salt -> (public address -> mapped address, used mapped addresses), for the life of the process.
_PUBLIC_TABLES: dict[bytes, tuple[dict[int, int], set[int]]] = {}


def scrub(obj: Any, *, salt: str) -> Any:
    """Return a scrubbed deep copy of ``obj`` (JSON-like data). See the module docstring."""
    return _Scrubber(salt.encode()).walk(obj, None)


def drop_key(key: str) -> bool:
    """True if a key names a secret or hardware identity that must not appear in fixtures.

    ``key`` is matched as a word (``apiKey``, ``x_iapp_key``, ``wpakey``) so that ``keyword``,
    ``keyboard`` or ``keepalive`` are kept; every legacy ``x_*`` field is a hidden secret. Location
    keys (``geo_info``, ``latitude``, ``city``, ``isp_name``, ...) are dropped too.
    """
    lower = key.lower()
    if lower.startswith("x_") or any(s in lower for s in _DROP_SUBSTRINGS):
        return True
    words = [m.lower() for m in _KEY_WORDS.findall(key)]
    if any(w.endswith(("key", "keys")) for w in words):
        return True
    if lower.replace("_", "").replace("-", "") in _LOCATION_KEYS:
        return True
    return bool(words) and (words[0] in _LOCATION_WORDS or words[0].startswith(_LOCATION_PREFIXES))


def _identity_kind(key: str) -> str | None:
    """The ``<kind>`` prefix for a key whose string values identify a person, place or device; else None."""
    joined = key.lower().replace("_", "").replace("-", "")
    if kind := _IDENTITY_KEYS.get(joined):
        return kind
    for word in (m.lower() for m in _KEY_WORDS.findall(key)):
        if kind := _IDENTITY_WORDS.get(word):
            return kind
        for prefix, kind in _IDENTITY_PREFIXES:
            if word.startswith(prefix):
                return kind
    if joined.endswith("hostname"):
        return "host"
    if joined.endswith("username"):
        return "user"
    return "name" if joined.endswith("name") else None


class _Scrubber:
    def __init__(self, salt: bytes) -> None:
        self.salt = salt
        self.public = _PUBLIC_TABLES.setdefault(salt, ({}, set()))

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
            kind = _identity_kind(key) if key else None
            if kind and obj:
                return f"{kind}-{self.digest('name', obj).hex()[:6]}"
            return self.text(obj)
        return obj

    # -- strings ---------------------------------------------------------------------------

    def text(self, value: str) -> str:
        whole = self.whole_ip(value)
        if whole is not None:
            return whole
        return _TOKEN_RE.sub(self.token, value)

    def token(self, m: re.Match[str]) -> str:
        value = m.group(0)
        if m.group("uuid"):
            return self.uuid(value)
        if m.group("ipv6"):
            # "::"-bearing text that is not an address: scrub the pieces (a MAC may follow the "::")
            return self.whole_ip(value) or "::".join(self.text(p) for p in value.split("::"))
        if m.group("mac"):
            return self.mac(value, m.group("sep"))
        if m.group("macdot"):
            return self.mac(value, ".")
        if m.group("macbare"):
            return self.mac(value, "")
        return self.ipv4_text(value)

    def uuid(self, value: str) -> str:
        return str(uuid.UUID(bytes=self.digest("uuid", value.lower())[:16], version=5))

    def mac(self, value: str, sep: str) -> str:
        """A locally administered MAC in the input's notation; every notation of one MAC maps alike."""
        digits = re.sub("[^0-9a-f]", "", value.lower())
        norm = ":".join(digits[i : i + 2] for i in range(0, 12, 2))
        out = "02" + self.digest("mac", norm)[:5].hex()
        if sep == ".":
            return ".".join(out[i : i + 4] for i in range(0, 12, 4))
        return sep.join(out[i : i + 2] for i in range(0, 12, 2))

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
        return ipaddress.IPv4Address(self.public_v4(packed))

    def public_v4(self, packed: int) -> int:
        """Injective within the process: a keyed slot, then the next free one on collision."""
        table, used = self.public
        if (mapped := table.get(packed)) is not None:
            return mapped
        n = len(_PUBLIC_SLOTS)
        if len(used) < n:
            start = int.from_bytes(self.digest("ipv4", str(ipaddress.IPv4Address(packed)))[:4], "big") % n
            mapped = next(s for s in (_PUBLIC_SLOTS[(start + i) % n] for i in range(n)) if s not in used)
        else:
            mapped = int(_OVERFLOW.network_address) + 1 + len(used) - n
        table[packed] = mapped
        used.add(mapped)
        return mapped

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
