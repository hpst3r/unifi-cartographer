# Brief W3 - render: render/drawio.py

Read `docs/WORKER.md`, `docs/SPEC.md` ("Render" section), `docs/MODEL.md`, `src/unifi_carto/model.py` first. Then read the draw.io skill you must follow: `~/.hermes/skills/creative/drawio-diagrams/SKILL.md`, `references/network-essentials.md` (incl. "Port-level cabling"), `references/rack-elevations.md`, and look at `templates/network-topology.drawio`, `templates/rack-office-unifi.drawio` (page 2 logical cabling), `templates/rack-pve-iscsi.drawio` (pages 3-4: cable layout + schedule) and `templates/cable-layout-generator.example.py` (lane routing). These are the user-approved house style: hostname with the management IP in small gray text under it, Cisco 2019 icons (`shape=mxgraph.cisco19.*`), containers for groups, legends, cables colored by purpose. The helper `python3 ~/.hermes/skills/creative/drawio-diagrams/scripts/drawio_helper.py` provides `check` and `draft` (draw.io Desktop 32.4.1 Flatpak; files must be under $HOME; spell out the absolute helper path).

## Files you own

`src/unifi_carto/render/__init__.py`, `src/unifi_carto/render/drawio.py` (split into a few modules under `render/` if it grows), `tests/test_render.py`, `tests/fixtures/render/*.json` (synthetic `Inventory` JSON you create).

## API

`def render_drawio(inv: Inventory, *, pages: Sequence[str] = ("logical", "cables", "schedule", "inventory"), site_ids: Sequence[str] | None = None) -> str` returning a complete `<mxfile>` document (stdlib `xml.etree` or careful escaping; must pass the skill helper's `check`). One set of pages per site, page names `"<site name> - Logical"`, `"... - Cables"`, `"... - Cable schedule"`, `"... - Inventory"`. Pure: no I/O, deterministic output for the same input (stable ids derived from model ids, no random/time).

## Pages

- **Logical**: tiers by role and uplink depth (gateway top; then switches by depth; APs at the bottom), deterministic grid placement (no CLI layout pass; the renderer computes coordinates), siblings ordered by name. Icons: gateway -> cisco19 firewall/router style, switch -> l2/l3 switch (`l3_switch` for aggregation/devices with downstream switches), AP -> `wireless_access_point2`, unknown -> generic. Label: `name` (or model + MAC suffix when unnamed), then gray IP. Links: label with ports when known (`p49 - p1` or `p49 - ?`), LAG members drawn as one bold edge labeled `LAG p49-50 - p1-2`, wireless/mesh dotted, external LLDP peers as small gray boxes. Legend for link styles; VLAN legend from `inv.networks` (name, VLAN, subnet). A page note counts links lacking port data ("N links without port numbers - legacy data unavailable") when applicable.
- **Cables**: only links with both port indices. Switch faceplates with used ports drawn in physical order (odd top, even bottom), free ports collapsed, cables colored by purpose: uplink-to-gateway, inter-switch trunk, AP, LAG member, LLDP/other (define the palette, add the legend). Follow the skill's "Port-level cabling" rules (lane waypoints, `jumpStyle=arc`). Keep it readable for up to ~6 switches and ~40 cables; beyond that, split into multiple pages per site.
- **Cable schedule**: HTML table (as in the template): cable id (`C-001` ... in deterministic order), color swatch, A device:port (name), B device:port, speed, LAG, native/tagged VLANs of the A port, source (official/legacy/lldp) so the reader sees what is verified.
- **Inventory**: HTML table of devices: name, model, role, IP, MAC, firmware, state, port count, uplink (device:port). (A rack elevation stays manual; mention that in a small note.)

## Tests and visual verification

- Synthetic inventories: (a) the topology from W2's planned fixture (UDM-Pro, agg switch, 2 access switches with a LAG, 2 APs one meshed, LLDP server; networks Default/Staff 10/Voice 20/Guest 30) built directly as `Inventory` objects in a fixture JSON; (b) the same with no port data (official-only); (c) a larger one (1 gateway, 2 agg, 6 access, 10 APs) to test spacing.
- Unit tests: output parses as XML; page names; deterministic (render twice -> identical string); every link appears once in logical and schedule; no `<!--` comments; ids unique; escapes `&`/`<` in names.
- Integration (skip unless `UNIFI_CARTO_DRAWIO_IT=1`): run the skill helper `check` on rendered files and `draft --all-pages`. **You must actually look at the drafts** (Read the PNGs) for all three inventories, fix overlaps/lines through labels, and report the final visual verdict per page in `verified`. Put drafts and rendered files under `~/.hermes/work/unifi-cartographer/scratch/w3/` (not in the repo).

## Gate

ruff format/check, mypy strict, pytest all green; commit. Report public API, page-level visual verdicts, and any house-style conventions you had to invent (so they can be folded into the skill).
