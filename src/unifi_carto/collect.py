"""Orchestration: discover consoles, collect official + legacy snapshots per site, build the inventory.

Owned by the parent (see docs/SPEC.md). Network I/O happens only through ``client_factory`` transports.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from unifi_carto import __version__
from unifi_carto.build import build_inventory
from unifi_carto.config import Config, ConsoleConfig, TlsConfig
from unifi_carto.errors import ApiError, AuthRefused, CartoError, ConfigError
from unifi_carto.http import ReadOnlyClient
from unifi_carto.integration import Integration
from unifi_carto.legacy import Legacy
from unifi_carto.model import CollectionWarning, Inventory
from unifi_carto.raw import ConsoleRef, LegacySnapshot, OfficialSnapshot
from unifi_carto.sitemanager import BASE_URL as SITE_MANAGER_URL
from unifi_carto.sitemanager import SiteManager, connector_base_url, host_summary
from unifi_carto.transport import Transport

log = logging.getLogger(__name__)

ClientFactory = Callable[..., Transport]


@dataclass
class Target:
    """One console to collect, with the access path chosen for it."""

    ref: ConsoleRef
    conf: ConsoleConfig | None  # None = discovered via Site Manager only
    official_base: str
    official_key: str
    official_key_header: str
    tls: TlsConfig | None


@dataclass
class Result:
    inventory: Inventory
    exit_code: int  # 0 ok, 1 partial, 3 nothing collected
    official: list[OfficialSnapshot] = field(default_factory=list)
    legacy: dict[tuple[str, str], LegacySnapshot] = field(default_factory=dict)


def _w(code: str, message: str, console: str | None = None, site: str | None = None) -> CollectionWarning:
    return CollectionWarning(code=code, message=message, console=console, site=site)


def plan_targets(
    cfg: Config,
    *,
    console_names: Sequence[str] = (),
    client_factory: ClientFactory = ReadOnlyClient,
    warnings: list[CollectionWarning],
) -> list[Target]:
    """Resolve configured and Site Manager-discovered consoles into collection targets."""
    sm_key = cfg.site_manager_key.resolve("site manager API key")
    discovered: dict[str, ConsoleRef] = {}
    if sm_key:
        with _closing(client_factory(SITE_MANAGER_URL, api_key=sm_key, api_key_header="X-API-Key")) as sm_t:
            for host in SiteManager(sm_t).hosts():
                try:
                    found = host_summary(host)
                except ApiError as exc:
                    warnings.append(_w("parse_error", f"Site Manager host skipped: {exc}"))
                    continue
                discovered[found.id] = found

    by_name = {ref.name: ref for ref in discovered.values()}
    targets: list[Target] = []
    used: set[str] = set()
    for conf in cfg.consoles:
        ref = discovered.get(conf.host_id or "") or by_name.get(conf.name)
        if ref is not None:
            used.add(ref.id)
        if conf.local_url:
            key = conf.api_key.resolve(f"console {conf.name} API key")
            if not key:
                raise ConfigError(f"console {conf.name}: local_url needs api_key_env or api_key_file")
            local = ConsoleRef(
                id=ref.id if ref else conf.name,
                name=conf.name,
                access="local",
                local_url=conf.local_url,
                model=ref.model if ref else None,
                firmware=ref.firmware if ref else None,
            )
            targets.append(
                Target(local, conf, f"{conf.local_url}/proxy/network/integration", key, "X-API-KEY", conf.tls)
            )
        elif ref is not None and sm_key:
            targets.append(Target(ref, conf, connector_base_url(ref.id), sm_key, "X-API-Key", None))
        else:
            warnings.append(
                _w("console_skipped", "no local_url and not found via Site Manager; nothing to collect", conf.name)
            )
    if sm_key:
        for ref in discovered.values():
            if ref.id not in used:
                targets.append(Target(ref, None, connector_base_url(ref.id), sm_key, "X-API-Key", None))
    if console_names:
        wanted = set(console_names)
        targets = [t for t in targets if t.ref.name in wanted or t.ref.id in wanted]
        missing = wanted - {t.ref.name for t in targets} - {t.ref.id for t in targets}
        if missing:
            raise ConfigError(f"unknown console(s): {sorted(missing)}")
    return targets


def collect(
    cfg: Config,
    *,
    console_names: Sequence[str] = (),
    site_names: Sequence[str] = (),
    legacy: bool = True,
    include_clients: bool = False,
    client_factory: ClientFactory = ReadOnlyClient,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Result:
    warnings: list[CollectionWarning] = []
    targets = plan_targets(cfg, console_names=console_names, client_factory=client_factory, warnings=warnings)
    official: list[OfficialSnapshot] = []
    legacy_snaps: dict[tuple[str, str], LegacySnapshot] = {}
    failures = 0

    for t in targets:
        name = t.ref.name
        target_snaps: list[OfficialSnapshot] = []
        try:
            with _closing(
                client_factory(t.official_base, api_key=t.official_key, api_key_header=t.official_key_header, tls=t.tls)
            ) as tr:
                api = Integration(tr)
                info = api.info()
                version = info.get("applicationVersion") if isinstance(info, dict) else None
                sites = api.sites()
                if site_names:
                    sites = [s for s in sites if _site_matches(s, site_names)]
                for site in sites:
                    try:
                        snap = api.collect_site(t.ref, site, include_clients=include_clients)
                    except ApiError as exc:
                        failures += 1
                        warnings.append(_w("site_failed", _api_msg(exc), name, _label(site)))
                        continue
                    snap.app_version = version if isinstance(version, str) else None
                    target_snaps.append(snap)
        except AuthRefused as exc:
            failures += 1
            warnings.append(_w("console_auth_refused", _api_msg(exc), name))
            continue
        except ApiError as exc:
            if t.ref.access == "connector" and t.conf is None and exc.status == 404:
                # Discovered host without UniFi Network (e.g. a Protect-only NVR): note it, not a failure.
                warnings.append(_w("console_not_network", _api_msg(exc), name))
            else:
                failures += 1
                warnings.append(_w("console_failed", _api_msg(exc), name))
            continue
        finally:
            official.extend(target_snaps)

        if not legacy:
            continue
        if t.ref.access != "local" or t.conf is None or not t.conf.local_url:
            warnings.append(
                _w("legacy_skipped_connector", "port-level data needs a local_url (legacy API is LAN/VPN only)", name)
            )
            continue
        legacy_snaps.update(_collect_legacy(t, target_snaps, include_clients, warnings, client_factory))

    inv = build_inventory(
        official,
        legacy_snaps,
        collector_version=__version__,
        generated_at=now(),
        include_clients=include_clients,
        extra_warnings=warnings,
    )
    if not legacy:
        inv.consoles = [c.model_copy(update={"legacy": "disabled"}) for c in inv.consoles]
    if not official:
        code = 3
    elif failures or any(w.code in _PARTIAL_CODES for w in inv.warnings):
        code = 1
    else:
        code = 0
    return Result(inventory=inv, exit_code=code, official=official, legacy=legacy_snaps)


_PARTIAL_CODES = frozenset({"site_failed", "console_failed", "console_auth_refused", "console_skipped"})


def _collect_legacy(
    t: Target,
    snaps: list[OfficialSnapshot],
    include_clients: bool,
    warnings: list[CollectionWarning],
    client_factory: ClientFactory,
) -> dict[tuple[str, str], LegacySnapshot]:
    assert t.conf is not None and t.conf.local_url
    out: dict[tuple[str, str], LegacySnapshot] = {}
    creds: tuple[str, str] | None = None
    if t.conf.legacy_username.configured or t.conf.legacy_password.configured:
        user = t.conf.legacy_username.resolve(f"console {t.ref.name} legacy username")
        pw = t.conf.legacy_password.resolve(f"console {t.ref.name} legacy password")
        if not user or not pw:
            raise ConfigError(f"console {t.ref.name}: legacy username and password must both be configured")
        creds = (user, pw)
    with _closing(client_factory(f"{t.conf.local_url}/proxy/network", api_key=t.official_key, tls=t.tls)) as tr:
        leg = Legacy(tr, console=t.ref.name)
        for snap in snaps:
            short = snap.site.get("internalReference")
            if not isinstance(short, str) or not short:
                warnings.append(
                    _w(
                        "legacy_site_unmapped",
                        "site has no internalReference; legacy skipped",
                        t.ref.name,
                        _label(snap.site),
                    )
                )
                continue
            got = leg.collect_site(short, include_clients=include_clients, credentials=creds)
            if got is None:
                warnings.extend(leg.last_warnings)
                continue
            out[(snap.console.id, str(snap.site["id"]))] = got
    return out


class _closing:
    """Context manager that closes a transport if it has ``close()`` (fakes may not)."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def __enter__(self) -> Transport:
        return self.transport

    def __exit__(self, *exc: object) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()


def _site_matches(site: dict[str, Any], names: Sequence[str]) -> bool:
    return any(site.get(k) in names for k in ("id", "name", "internalReference"))


def _label(site: dict[str, Any]) -> str | None:
    for k in ("name", "internalReference", "id"):
        v = site.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _api_msg(exc: CartoError) -> str:
    status = getattr(exc, "status", None)
    return f"{exc} (HTTP {status})" if status else str(exc)


__all__ = ["Result", "Target", "collect", "plan_targets"]
