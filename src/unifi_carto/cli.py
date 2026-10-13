"""unifi-carto: read-only UniFi Network inventory and topology collector."""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from unifi_carto import __version__
from unifi_carto.config import load_config
from unifi_carto.errors import CartoError, ConfigError
from unifi_carto.model import Inventory, json_schema

PAGE_CHOICES = ("logical", "cables", "schedule", "inventory")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="unifi-carto", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--debug", action="store_true", help="debug logging (paths and status codes only; never secrets)")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="collect an inventory JSON")
    c.add_argument("-c", "--config", type=Path, help="config TOML (see examples/config.example.toml)")
    c.add_argument("-o", "--output", type=Path, default=Path("inventory.json"))
    c.add_argument("--console", action="append", default=[], help="limit to console name/id (repeatable)")
    c.add_argument("--site", action="append", default=[], help="limit to site name/id/internal ref (repeatable)")
    c.add_argument("--no-legacy", action="store_true", help="official API only (no port-level data)")
    c.add_argument("--clients", action="store_true", help="include client devices (personal data)")

    r = sub.add_parser("render", help="render an inventory JSON to draw.io")
    r.add_argument("inventory", type=Path)
    r.add_argument("-o", "--output", type=Path, required=True)
    r.add_argument("--pages", default=",".join(PAGE_CHOICES), help=f"comma list of {', '.join(PAGE_CHOICES)}")
    r.add_argument("--site", action="append", default=[], help="limit to site id (repeatable)")

    pin = sub.add_parser("pin", help="print the SHA-256 fingerprint of a console's TLS certificate")
    pin.add_argument("url")

    cap = sub.add_parser("capture", help="write scrubbed raw API responses as test fixtures")
    cap.add_argument("-c", "--config", type=Path)
    cap.add_argument("-o", "--output", type=Path, required=True)
    cap.add_argument("--console", action="append", default=[])
    cap.add_argument("--site", action="append", default=[])
    cap.add_argument("--no-legacy", action="store_true")
    cap.add_argument("--salt", help="scrub salt (default: random per capture)")

    sub.add_parser("schema", help="print the inventory JSON Schema")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return _dispatch(args)
    except ConfigError as exc:
        print(f"unifi-carto: config error: {exc}", file=sys.stderr)
        return 2
    except CartoError as exc:
        print(f"unifi-carto: {exc}", file=sys.stderr)
        return 3


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "schema":
        json.dump(json_schema(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    if args.command == "pin":
        from unifi_carto.http import fingerprint

        try:
            print(fingerprint(args.url))
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        except OSError as exc:
            raise CartoError(f"cannot fetch TLS certificate from {args.url}: {exc.strerror or exc}") from None
        return 0
    if args.command == "render":
        from unifi_carto.render import render_drawio

        try:
            inv = Inventory.model_validate_json(args.inventory.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read inventory {args.inventory}: {exc.strerror}") from None
        except ValidationError as exc:
            raise ConfigError(f"{args.inventory} is not a valid inventory ({exc.error_count()} error(s))") from None
        pages = [p.strip() for p in args.pages.split(",") if p.strip()]
        try:
            xml = render_drawio(inv, pages=pages, site_ids=args.site or None)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        args.output.write_text(xml, encoding="utf-8")
        print(f"wrote {args.output}")
        return 0
    if args.command == "collect":
        from unifi_carto.collect import collect

        result = collect(
            load_config(args.config),
            console_names=args.console,
            site_names=args.site,
            legacy=not args.no_legacy,
            include_clients=args.clients,
        )
        args.output.write_text(result.inventory.model_dump_json(indent=2) + "\n", encoding="utf-8")
        _summary(result.inventory, args.output)
        return result.exit_code
    if args.command == "capture":
        return _capture(args)
    return 2  # pragma: no cover - argparse enforces the choices


def _summary(inv: Inventory, path: Path) -> None:
    ported = sum(1 for link in inv.links if link.a.port_idx and link.b.port_idx)
    print(
        f"wrote {path}: {len(inv.consoles)} console(s), {len(inv.sites)} site(s), {len(inv.devices)} device(s), "
        f"{len(inv.links)} link(s) ({ported} with ports on both ends), {len(inv.warnings)} warning(s)"
    )
    for w in inv.warnings:
        where = "/".join(x for x in (w.console, w.site) if x)
        print(f"  warning [{w.code}] {where}: {w.message}", file=sys.stderr)


def _capture(args: argparse.Namespace) -> int:
    """Collect, then write raw snapshots scrubbed for use as public test fixtures.

    Directory names are positional (console-1/site-1) so real console and site names never reach paths.
    Clients are never captured.
    """
    from unifi_carto.collect import collect
    from unifi_carto.scrub import scrub

    salt = args.salt or secrets.token_hex(16)
    result = collect(
        load_config(args.config),
        console_names=args.console,
        site_names=args.site,
        legacy=not args.no_legacy,
        include_clients=False,
    )
    consoles: dict[str, int] = {}
    sites: dict[str, int] = {}
    written = 0
    for snap in result.official:
        ci = consoles.setdefault(snap.console.id, len(consoles) + 1)
        si = sites.setdefault(f"{snap.console.id}/{snap.site.get('id')}", len(sites) + 1)
        site_dir = args.output / f"console-{ci}" / f"site-{si}"
        official_files: dict[str, Any] = {
            # the id is a Site Manager host id (hardware-derived) or the configured name: positional instead
            "console.json": {**asdict(snap.console), "id": f"console-{ci}"},
            "site.json": snap.site,
            "info.json": {"applicationVersion": snap.app_version},
            "devices.json": snap.devices,
            "device_details.json": snap.device_details,
            "networks.json": snap.networks,
            "wlans.json": snap.wlans,
            "wans.json": snap.wans,
            "lags.json": snap.lags,
            "mc_lag_domains.json": snap.mc_lag_domains,
            "switch_stacks.json": snap.switch_stacks,
        }
        written += _write_scrubbed(site_dir / "official", official_files, salt, scrub)
        leg = result.legacy.get((snap.console.id, str(snap.site["id"])))
        if leg is not None:
            legacy_files: dict[str, Any] = {
                "stat_device.json": leg.devices,
                "rest_portconf.json": leg.portconfs,
                "rest_networkconf.json": leg.networkconfs,
                "rest_wlanconf.json": leg.wlanconfs,
            }
            written += _write_scrubbed(site_dir / "legacy", legacy_files, salt, scrub)
    print(f"wrote {written} scrubbed file(s) under {args.output}; review them before committing")
    return result.exit_code


def _write_scrubbed(directory: Path, files: dict[str, Any], salt: str, scrub: Callable[..., Any]) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (directory / name).write_text(json.dumps(scrub(data, salt=salt), indent=2, sort_keys=True) + "\n")
    return len(files)


if __name__ == "__main__":
    raise SystemExit(main())
