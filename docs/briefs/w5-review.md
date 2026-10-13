# Brief W5 - adversarial review (read-only for production code)

You are the reviewer. Find real defects in `src/unifi_carto/**` against `docs/SPEC.md` (incl. Amendment 1), `docs/MODEL.md` and the vendor API facts. Do **not** fix production code. Prove each finding with a test.

## Files you own

`tests/review/__init__.py`, `tests/review/conftest.py`, `tests/review/test_review_*.py`, and a report `docs/reviews/2026-10-review-1.md`.

Each proof test calls a helper `review_bug("ID: one-line reason")` at its top, defined in `tests/review/conftest.py`, which skips unless `UNIFI_CARTO_REVIEW=1`. With the env var set the test must FAIL on the current code (that is the proof) and pass once fixed. Assert the observable symptom (wrong output, leaked string, request sent), not the mechanism, so a correct fix makes it pass without rewriting the test. Non-defect observations go only in the report.

## Focus (in priority order)

1. **Read-only guarantee:** any way for a non-GET request to leave the process (other than the sanctioned login), incl. redirects that change method, HEAD/OPTIONS, httpx event hooks, subclassing, `Integration`/`Legacy` misuse, the `test_no_write_calls_outside_http` scanner being bypassable by obvious spellings.
2. **Secret leakage:** API keys, Site Manager key, legacy password, TOKEN cookie, CSRF token in: exceptions (`str`/`repr`), logs at DEBUG, `Inventory` JSON (e.g. legacy `x_passphrase` or WAN `raw` carrying PPPoE passwords / RADIUS secrets!), `capture` output, the CLI's warning output, URLs (query strings). `Wan.raw` is a pass-through dict - check what the official WAN object can contain.
3. **TLS:** pin comparison correctness (case, prefix, colons, SHA-1 vs SHA-256 confusion), TOCTOU between fingerprint fetch and connection, behavior with a CA file that is a directory, `trust_env`, SNI/hostname when `local_url` is an IP.
4. **Scrubber completeness:** MACs in other notations (dashes, no separators, uppercase, inside longer strings like `"eth0 (AA:BB:...)"`), IPs inside CIDR strings and URLs, hostnames/SSIDs under keys not in the list (`desc`, `alias`, `display_name`, `lldp` `system_name`, `port_table[].name`), serials, coordinates/addresses, site `desc`, admin emails, anything identifying in `device_details`. Grep the synthetic fixtures for fields a real console returns and think about what a real capture would leak.
5. **Builder correctness:** link dedup when both ends report, LAG with LLDP on only one member, mesh, devices without MAC, duplicate MACs across sites, legacy site without matching official site, VLAN resolution order (port override vs profile vs default), `tagged_vlans` semantics with `custom` + native, deterministic ordering with unicode names, `None` vs `[]` meanings.
6. **Robustness:** pagination loops (`totalCount` lies, `data` not a list), huge responses, `Retry-After` abuse (very large, negative), thread-pool error handling in `collect_site`, `collect.py` behavior when a console appears twice (configured by name and discovered by id), Site Manager host name collisions.
7. **Renderer:** XML injection via device/site/VLAN names (`"`, `<`, `]]>`, control chars), HTML labels (`html=1` means names are interpreted as HTML: a device named `<img src=x onerror=...>` must render as text), very long names, 0 devices, devices without IPs.

Also check vendor facts where the code depends on them (WebFetch allowed): the mirrored OpenAPI at `https://raw.githubusercontent.com/opastorello/unifi-api-docs/main/network/v10.6.106/openapi.json` (WAN schema, client schema, device detail), unpoller structs (`https://github.com/unpoller/unifi`) for legacy fields.

## Report

`docs/reviews/2026-10-review-1.md`: one line per finding: `[SEVERITY] ID path:line - problem - evidence (test name) - suggested fix`, severities CRITICAL/HIGH/MEDIUM/LOW, then a short "checked and fine" list. Gate: with `UNIFI_CARTO_REVIEW` unset, the full suite is green; with it set, exactly your proof tests fail. Commit on your branch. In the final JSON, put findings (same one-line format) in `verified`.
