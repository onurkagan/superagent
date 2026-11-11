"""A small HTTP server for the panel: one HTML page and a JSON API, standard library only.

It binds to localhost by default. Set ``[panel] token`` before binding it anywhere else: every request must then
carry the token, and actions such as stopping a ticket are refused without it.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from superagent.panel.data import PanelData

log = logging.getLogger(__name__)

_TICKET_ACTION = re.compile(r"^/api/tickets/([\w-]+)/(stop|retry)$")
_TICKET = re.compile(r"^/api/tickets/([\w-]+)$")
_REEVALUATE = re.compile(r"^/api/triggers/(.+)/reevaluate$")


def make_handler(data: PanelData, token: str = "") -> type[BaseHTTPRequestHandler]:
    page = resources.files("superagent").joinpath("panel", "page.html").read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "superagent-panel"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature from the base class
            log.debug("panel: " + format, *args)

        def _authorized(self, query: dict[str, list[str]]) -> bool:
            if not token:
                return True
            supplied = self.headers.get("X-Superagent-Token") or (query.get("token") or [""])[0]
            return hmac.compare_digest(supplied.encode(), token.encode())

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def do_GET(self) -> None:  # noqa: N802 - name from the base class
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if not self._authorized(query):
                return self._error(HTTPStatus.UNAUTHORIZED, "a valid token is required")
            path = url.path
            if path in ("/", "/index.html"):
                return self._send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")
            if path == "/api/overview":
                return self._json(data.overview())
            if path == "/api/tickets":
                return self._json(data.tickets((query.get("filter") or ["open"])[0]))
            match = _TICKET.match(path)
            if match:
                ticket = data.ticket(match.group(1))
                return self._json(ticket) if ticket else self._error(HTTPStatus.NOT_FOUND, "no such ticket")
            if path == "/api/decisions":
                return self._json(data.decisions())
            if path == "/api/threads":
                return self._json(data.threads((query.get("filter") or ["subscribed"])[0]))
            if path == "/api/events":
                return self._json(data.events())
            return self._error(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if not self._authorized(parse_qs(url.query)):
                return self._error(HTTPStatus.UNAUTHORIZED, "a valid token is required")
            if self.headers.get("X-Requested-By") != "superagent-panel":
                return self._error(HTTPStatus.FORBIDDEN, "missing X-Requested-By header")
            path = url.path
            match = _TICKET_ACTION.match(path)
            if match:
                data.command(match.group(2), ticket=match.group(1))
                return self._json({"queued": match.group(2)}, HTTPStatus.ACCEPTED)
            if path in ("/api/pause", "/api/resume"):
                data.command(path.rsplit("/", 1)[1])
                return self._json({"queued": path.rsplit("/", 1)[1]}, HTTPStatus.ACCEPTED)
            match = _REEVALUATE.match(path)
            if match:
                data.command("reevaluate", trigger=match.group(1))
                return self._json({"queued": "reevaluate"}, HTTPStatus.ACCEPTED)
            return self._error(HTTPStatus.NOT_FOUND, "not found")

    return Handler


class PanelServer:
    def __init__(self, data: PanelData, host: str = "127.0.0.1", port: int = 8765, token: str = "") -> None:
        if host not in ("127.0.0.1", "localhost", "::1") and not token:
            raise ValueError("set [panel] token before binding the panel to a non-local address")
        self.httpd = ThreadingHTTPServer((host, port), make_handler(data, token))
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="panel", daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
