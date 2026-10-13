# Brief W4 - end-to-end tests: collect.py and cli.py

Read `docs/WORKER.md`, `docs/SPEC.md` (incl. Amendment 1), `docs/MODEL.md`, then `src/unifi_carto/collect.py`, `cli.py`, and skim `http.py`, `integration.py`, `sitemanager.py`, `legacy.py`, `build.py`, `scrub.py`, `render/`. Existing synthetic fixtures: `tests/fixtures/synthetic/site-a/` (+ `make_site_a.py`), loaders in `tests/conftest.py`.

## Files you own

`tests/test_collect.py`, `tests/test_cli.py`, `tests/fixtures/e2e/**` (new). You may fix **bugs** in `src/unifi_carto/collect.py` and `src/unifi_carto/cli.py` (parent-owned, so keep fixes minimal and list each one with the failing test that proves it under `contract_issues`, prefixed `FIXED:`). Bugs elsewhere: write the failing test as `pytest.mark.xfail(strict=True, reason="BUG: ...")` and report it; do not edit other modules.

## Scenarios (respx-mocked HTTP through the real `ReadOnlyClient`, no network)

1. Site Manager discovery with three hosts: `hq` configured with `local_url` (local official + legacy via API key), `branch` not configured (connector, official only, `legacy_skipped_connector` warning), `nvr` not configured whose connector integration path 404s (`console_not_network`, run still exit 0). Inventory has 2 consoles, correct `access`, links with ports for hq only.
2. Local console where legacy refuses the API key (401) and `legacy_username_env`/`legacy_password_file` are configured: login POST happens exactly once to `https://<host>/api/auth/login` (origin, not under /proxy/network), subsequent legacy GETs carry the cookie and not the API key; `Console.legacy == "login"`.
3. Same but no legacy credentials: warning `legacy_auth_refused`, official-only links, exit code 0 or 1 per SPEC (decide from the SPEC; if the SPEC is ambiguous, report it).
4. `--no-legacy`: no legacy requests at all (assert respx saw none), `Console.legacy == "disabled"`.
5. Official API 401 on one of two consoles: exit 1, inventory written with the other console; 401 on all: exit 3.
6. `--console` / `--site` filters, unknown console -> exit 2 with a config error.
7. Config errors: missing env var for a key -> exit 2, message names the variable and contains no secret.
8. **Read-only end to end:** across all scenarios, assert every request respx saw is GET except the single login POST in scenario 2.
9. **Secrets end to end:** with DEBUG logging (`--debug`) and `capsys`/`caplog`, the API keys, Site Manager key, legacy password and session cookie never appear in stdout, stderr, logs or the written inventory JSON.
10. `capture`: writes `console-N/site-N/{official,legacy}/*.json`; none of the original MACs, device names, site names, IPs, the API key or SSIDs from the fixtures appear in any captured file (grep the whole output tree); fixture files load back through `tests/conftest.py`-style loaders and `build_inventory` produces the same number of devices and links as the unscrubbed run (joins survive scrubbing).
11. `collect` -> `render` round trip through `cli.main`: the drawio output parses as XML and contains one diagram per site and page kind.
12. `pin` against a loopback trustme HTTPS server prints `sha256:` + 32 colon-separated hex pairs matching the cert.

Build the extra fixtures by deriving from site-a (scripted under `tests/fixtures/e2e/`, regenerable); keep them synthetic.

## Gate

ruff format/check, mypy strict, full pytest green (xfail-strict bug tests allowed); commit on your branch.
