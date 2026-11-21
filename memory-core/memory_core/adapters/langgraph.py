"""LangGraph BaseStore backed by the scoped memory-core HTTP API."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from langgraph.store.base import BaseStore, GetOp, Item, ListNamespacesOp, PutOp, SearchItem, SearchOp

from .citation import guard
from .http.client import HTTPMemoryClient
from .ingest import AgentRequestError, SCOPE_FIELDS


class SearchResults(list):
    """BaseStore-compatible items with an explicit evidence verdict."""
    def __init__(self, items, result):
        super().__init__(items)
        self.state = result["state"]
        self.missing = result["missing"]
        self.allowed_raw_ids = list(result["allowed_raw_ids"])
        self.citation_policy = result.get("citation_policy")

    def as_dict(self):
        return {"state": self.state, "items": [item.dict() for item in self],
                "missing": self.missing, "allowed_raw_ids": self.allowed_raw_ids,
                "citation_policy": self.citation_policy}


class MemoryStore(BaseStore):
    def __init__(self, client=None):
        self.client = client if client is not None else HTTPMemoryClient.from_env()
        self.namespace = tuple(self.client.conversation[field] for field in SCOPE_FIELDS)

    def guard(self, reply, snapshot):
        return guard(reply, snapshot, client=self.client)

    def batch(self, ops):
        out = []
        for op in ops:
            if isinstance(op, SearchOp):
                result = self._search(op)
            elif isinstance(op, ListNamespacesOp):
                result = self._list_namespaces(op)
            elif isinstance(op, (GetOp, PutOp)):
                result = self._item_operation(op)
            else:
                raise AgentRequestError("unsupported_store_operation")
            out.append(result)
        return out

    async def abatch(self, ops):
        return await asyncio.to_thread(self.batch, list(ops))

    def _item_operation(self, op):
        if op.namespace != self.namespace:
            raise AgentRequestError("scope_forbidden", 403)
        data = {"conversation": self.client.conversation, "id": op.key, "by_message_id": True}
        if isinstance(op, GetOp):
            item = self.client.call("get", data)["item"]
            if item is None:
                return None
            stamp = datetime.fromisoformat(item["at"].replace("Z", "+00:00"))
            return Item(value=item, key=op.key, namespace=op.namespace, created_at=stamp, updated_at=stamp)
        if op.value is None:
            self.client.call("delete", data)
            return None
        if op.ttl is not None or op.index not in (None, False):
            raise AgentRequestError("unsupported_ttl_or_index")
        if not isinstance(op.value, dict):
            raise AgentRequestError("invalid_body")
        if op.value.get("message_id", op.key) != op.key:
            raise AgentRequestError("message_id_conflict", 409)
        self.client.call("remember", {**op.value, "message_id": op.key})
        return None

    def _search(self, op):
        if op.namespace_prefix != self.namespace[:len(op.namespace_prefix)]:
            raise AgentRequestError("scope_forbidden", 403)
        if op.offset != 0:
            raise AgentRequestError("unsupported_offset")
        if type(op.limit) is not int or op.limit < 1:
            raise AgentRequestError("invalid_limit")
        payload = {"conversation": self.client.conversation, "query": op.query, "limit": min(op.limit, 3)}
        if op.filter is not None:
            payload["filters"] = op.filter
        result = self.client.call("recall", payload)
        items = []
        for evidence in result["items"]:
            stamp = datetime.fromtimestamp(evidence["raw_ts"], timezone.utc)
            key = evidence.get("message_id") or evidence["raw_id"]
            items.append(SearchItem(self.namespace, key, evidence, stamp, stamp))
        return SearchResults(items, result)

    def _list_namespaces(self, op):
        namespace = self.namespace
        for condition in op.match_conditions or ():
            size = len(condition.path)
            sample = namespace[:size] if condition.match_type == "prefix" else namespace[len(namespace) - size:]
            if size > len(namespace) or any(p != "*" and p != actual for p, actual in zip(condition.path, sample)):
                return []
        matches = [namespace[:op.max_depth]]
        return matches[op.offset:op.offset + op.limit]
