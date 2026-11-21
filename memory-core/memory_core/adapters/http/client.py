"""Client and common memory-backend shape for the local memory HTTP service."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..ingest import AgentRequestError, SCOPE_FIELDS, conversation_scope, unavailable
from ..tools import OPERATIONS


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPMemoryClient:
    def __init__(self, url, token, conversation, *, timeout=8):
        conversation_scope(conversation)
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost")
                or parsed.username is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or not parsed.port):
            raise AgentRequestError("invalid_http_url")
        if not (isinstance(token, str) and 32 <= len(token) <= 256
                and token.isascii() and token.isprintable() and not any(c.isspace() for c in token)):
            raise AgentRequestError("http_token_required")
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        self.conversation = dict(conversation)
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_env(cls):
        conversation = {field: os.getenv("MEMORY_AGENT_" + field.upper()) for field in SCOPE_FIELDS}
        port = os.getenv("MEMORY_AGENT_HTTP_PORT", "")
        return cls(os.getenv("MEMORY_AGENT_HTTP_URL") or f"http://127.0.0.1:{port}",
                   os.getenv("MEMORY_AGENT_HTTP_TOKEN", ""), conversation)

    def call(self, name, data):
        if name not in OPERATIONS:
            raise AgentRequestError("unknown_tool", 404)
        route = "ingest" if name == "remember" else name
        request = Request(f"{self.url}/{route}", method="POST",
                          data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            if exc.code >= 500:
                exc.close()
                raise unavailable() from None
            try:
                with exc:
                    error = json.load(exc)
            except (ValueError, OSError):
                raise unavailable() from None
            if isinstance(error, dict) and isinstance(error.get("error"), str):
                raise AgentRequestError(error["error"], exc.code,
                                        **{k: v for k, v in error.items() if k not in ("error", "status")}) from None
            raise unavailable() from None
        except (URLError, OSError, ValueError):
            raise unavailable() from None
        if not isinstance(result, dict) or "error" in result:
            raise unavailable()
        if name in ("judge", "recall", "search") and (
                result.get("state") not in ("found", "suspected", "absent", "not_checked")
                or not isinstance(result.get("items"), list) or not isinstance(result.get("allowed_raw_ids"), list)
                or "missing" not in result):
            raise unavailable()
        return result


class MemoryBackend:
    """A conversation-bound add/search/get/delete interface over HTTP."""
    def __init__(self, client=None):
        self.client = client if client is not None else HTTPMemoryClient.from_env()

    def add(self, text, metadata):
        if not isinstance(metadata, dict):
            raise AgentRequestError("invalid_metadata")
        return self.client.call("add", {"text": text, "metadata": {
            "conversation": self.client.conversation, **metadata,
        }})

    def search(self, query, limit=3, filters=None):
        return self.client.call("search", {
            "conversation": self.client.conversation, "query": query, "limit": limit,
            **({"filters": filters} if filters is not None else {}),
        })

    def get(self, identifier):
        return self.client.call("get", {"conversation": self.client.conversation, "id": identifier})["item"]

    def delete(self, identifier):
        return self.client.call("delete", {"conversation": self.client.conversation, "id": identifier})
