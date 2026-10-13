# Worker rules (unifi-cartographer)

You are one of several parallel workers. Each works in its own git worktree on its own branch. Read `docs/SPEC.md` and `docs/MODEL.md` first, then your brief.

1. **Ownership.** Edit only the files your brief assigns, plus new test files and fixtures it names. Never edit core files: `src/unifi_carto/{model,raw,config,errors,transport,cli}.py`, `docs/SPEC.md`, `docs/MODEL.md`, `schema/`, `pyproject.toml`, `uv.lock`, `.github/`. If the contract is wrong or missing something, work around it locally if possible and report it under `contract_issues` with the exact change you propose. If you need a new dev dependency, report it; do not add it.
2. **Read-only by design.** No code path may issue PUT/PATCH/DELETE, and POST only in `http.py`'s `login_legacy`. Never disable TLS verification.
3. **Toolchain.** From your worktree root: `uv sync --all-groups` once, then `uv run ruff format .`, `uv run ruff check .`, `uv run mypy`, `uv run pytest`. All four must pass before you finish. mypy is strict on `src/`.
4. **Tests.** No network access in tests (use fakes/respx/trustme on localhost). Use synthetic data only: documentation IPs (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, or 10.x private), locally administered MACs (`02:00:00:...`), names like `usw-agg-a`. Never put real customer, Ubiquiti account or credential data anywhere.
5. **Resources.** You run under a 4 GB memory cap. Keep pytest runs bounded; no parallel test runners.
6. **Commit** your work on your branch when the gate passes: `git add -A && git commit -m "<module>: <summary>"` (one or a few commits). Do not push, merge or rebase.
7. **Final answer.** Return ONLY one raw JSON object matching the schema you were given (no prose, no code fence). Distinguish what you verified by running commands from assumptions.
