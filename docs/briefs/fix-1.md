# Fix wave 1 (review 1)

Read `docs/WORKER.md`, `docs/SPEC.md` **Amendment 2** (the decisions you implement exactly), and `docs/reviews/2026-10-review-1.md`. Proof tests live in `tests/review/`; run them with `UNIFI_CARTO_REVIEW=1 uv run pytest -o addopts="" tests/review -k <ids>`. For each finding you own: make its proof test pass, then **delete the `review_bug(...)` line** in that test so it runs in the normal suite. A proof test that asserts mechanism rather than the symptom may be strengthened (never weakened); list each such change under `contract_issues` prefixed `TEST-CHANGED:`. Add focused tests for amendment items that have no proof test.

Gate: ruff format/check, mypy strict, full pytest green with and without `UNIFI_CARTO_REVIEW=1` for your IDs; commit on your branch.

## f1-runtime: http.py, collect.py, cli.py, errors.py, config.py, transport.py docstrings, tests/test_http.py

Findings: SEC-01, SEC-03, COL-01, COL-02, COL-03, ROB-01, COL-04, TLS-01, RO-01, SCR-06; Amendment 2 items 1-5, 7-12, 14, 17. (You may edit core files `errors.py`/`config.py`/`transport.py` for these items only.)

## f2-scrub: scrub.py, tests/test_scrub.py

Findings: SCR-01, SCR-02, SCR-03, SCR-04, SCR-05; Amendment 2 item 13. Also verify the official WiFi broadcast detail's passphrase field and legacy `wlanconf.x_passphrase` are dropped (add tests).

## f3-model-render: build.py, render/**, tests/test_build.py, tests/test_render.py

Findings: SEC-02 (Wan.raw allow-list; import `scrub.drop_key` only, do not edit scrub.py), BLD-01, RND-01; Amendment 2 items 6, 15, 16.
