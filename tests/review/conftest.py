"""Adversarial review (W5): proof tests for defects found in src/unifi_carto.

Every proof test calls ``review_bug("ID: reason")`` first. Without ``UNIFI_CARTO_REVIEW=1`` the test is
skipped, so the normal suite stays green; with it set, each proof test fails on the code under review
and passes once the defect is fixed. Findings and suggested fixes: docs/reviews/2026-10-review-1.md.
"""

from __future__ import annotations

import os
import ssl
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import trustme


def review_bug(reason: str) -> None:
    """Skip unless UNIFI_CARTO_REVIEW=1; ``reason`` is ``"ID: one-line description"``."""
    if os.environ.get("UNIFI_CARTO_REVIEW") != "1":
        pytest.skip(f"review proof (set UNIFI_CARTO_REVIEW=1): {reason}")


Handler = Callable[[BaseHTTPRequestHandler], None]


class LocalTls:
    """A real HTTPS server on 127.0.0.1 (trustme CA) whose GET/POST handlers tests can replace."""

    def __init__(self, ca_file: Path, port: int) -> None:
        self.ca_file = ca_file
        self.port = port
        self.base = f"https://127.0.0.1:{port}"
        self.on_get: Handler = _empty_json
        self.on_post: Handler = _empty_json


def _empty_json(h: BaseHTTPRequestHandler) -> None:
    send(h, 200, b"{}")


def send(h: BaseHTTPRequestHandler, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


@pytest.fixture
def local_tls(tmp_path: Path) -> Iterator[LocalTls]:
    ca = trustme.CA()
    leaf = ca.issue_cert("127.0.0.1")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    leaf.configure_cert(ctx)
    ca_file = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_file))
    holder: list[LocalTls] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            holder[0].on_get(self)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            holder[0].on_post(self)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    server.daemon_threads = True
    server.handle_error = lambda *a: None  # type: ignore[method-assign]
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    holder.append(LocalTls(ca_file, server.server_address[1]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield holder[0]
    finally:
        server.shutdown()
        server.server_close()
