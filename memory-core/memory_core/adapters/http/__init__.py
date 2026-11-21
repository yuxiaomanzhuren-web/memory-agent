"""Optional loopback HTTP ingress for clients without MCP."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys

from ..ingest import AgentRequestError, unavailable
from ..runtime import open_memory
from ..tools import OPERATIONS, call_memory

ROUTES = {"/" + operation: operation for operation in OPERATIONS}
ROUTES["/ingest"] = "remember"


class IngestHandler(BaseHTTPRequestHandler):
    timeout = 10

    def do_POST(self):
        try:
            operation = ROUTES.get(self.path)
            if operation is None:
                raise AgentRequestError("not_found", 404)
            received = self.headers.get("Authorization", "").encode("utf-8")
            if not hmac.compare_digest(received, self.server.authorization):
                raise AgentRequestError("unauthorized", 401)
            content_lengths = self.headers.get_all("Content-Length") or []
            if len(content_lengths) != 1 or not content_lengths[0].isascii() or not content_lengths[0].isdecimal():
                raise AgentRequestError("invalid_body_size")
            size = int(content_lengths[0])
            if not 0 < size <= 256 * 1024:
                raise AgentRequestError("invalid_body_size")
            try:
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
            except (ValueError, UnicodeError):
                raise AgentRequestError("invalid_json") from None
            result = call_memory(self.server.memory, operation, payload)
        except AgentRequestError as exc:
            self.send_json(exc.status, exc.payload())
        except Exception as exc:
            print(f"[memory-agent] ingress failure: {type(exc).__name__}", file=sys.stderr, flush=True)
            self.send_json(503, unavailable().payload())
        else:
            self.send_json(200, result)

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            self.close_connection = True

    def log_message(self, *_):
        # HTTP provider tokens and arbitrary paths do not belong in access logs.
        pass


class IngestServer(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, port, token, memory):
        self.memory = memory
        self.authorization = f"Bearer {token}".encode("ascii")
        super().__init__(("127.0.0.1", port), IngestHandler)


def main():
    try:
        token = os.environ.get("MEMORY_AGENT_HTTP_TOKEN", "")
        if not (32 <= len(token) <= 256 and token.isascii() and token.isprintable() and not any(c.isspace() for c in token)):
            raise AgentRequestError("http_token_required")
        raw_port = os.environ.get("MEMORY_AGENT_HTTP_PORT", "")
        if not raw_port.isascii() or not raw_port.isdecimal() or not 1 <= int(raw_port) <= 65535:
            raise AgentRequestError("http_port_required")
        with open_memory() as memory, IngestServer(int(raw_port), token, memory) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (AgentRequestError, OSError) as exc:
        reason = exc.code if isinstance(exc, AgentRequestError) else type(exc).__name__
        print(f"[memory-agent] startup refused: {reason}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
