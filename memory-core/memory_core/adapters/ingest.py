"""The message contract and scoped memory operations shared by the adapters."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import threading
import time
from uuid import uuid4

from ..citation import CITATION_POLICY
from ..memory_delete import DeleteError, validated_memory_id
from ..memory_evidence import RETIRED_RAW, owns_memory, raw_session, scope_from, scope_key, valid_agent_id
from ..memory_gates import MemoryGates
from ..memory_pipeline import MemoryPipeline
from ..rawlog import RawLog, valid_speaker
from ..source_provenance import SOURCE_KINDS, classify_source
from ..store import Store
from ..turn_recall import TurnRecall, _assigned_records


SCOPE_FIELDS = ("source_id", "owner_id", "conversation_id")
MESSAGE_FIELDS = ("text", "speaker", "at", "conversation", "source_kind")
RFC3339 = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                     r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-5][0-9])")


class AgentRequestError(ValueError):
    def __init__(self, code, status=400, **detail):
        super().__init__(code)
        self.code, self.status, self.detail = code, status, detail

    def payload(self):
        return {"error": self.code, "status": self.status, **self.detail}


def unavailable():
    return AgentRequestError("memory_unavailable", 503, message="记忆不可用")


def conversation_scope(conversation):
    if not isinstance(conversation, dict) or set(conversation) != set(SCOPE_FIELDS):
        raise AgentRequestError("invalid_conversation")
    if not all(valid_agent_id(conversation[field]) for field in SCOPE_FIELDS):
        raise AgentRequestError("invalid_conversation")
    return scope_from({
        "bot_id": "agent:" + conversation["source_id"],
        "persona": conversation["owner_id"],
        "conv_key": "private:" + conversation["conversation_id"],
    })


def require_fields(data, fields, optional=()):
    if not isinstance(data, dict):
        raise AgentRequestError("invalid_body")
    for field in fields:
        if data.get(field) is None:
            raise AgentRequestError("missing_" + field, field=field)
    if data.keys() - (*fields, *optional):
        raise AgentRequestError("unknown_field")


def require_text(data, key, length):
    value = data[key]
    if not isinstance(value, str) or not value.strip() or len(value) > length:
        raise AgentRequestError("invalid_" + key, field=key)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise AgentRequestError("invalid_" + key, field=key) from None
    return value


def parse_at(at):
    if not isinstance(at, str) or not RFC3339.fullmatch(at) or at.endswith("-00:00"):
        raise AgentRequestError("invalid_at", field="at")
    try:
        stamp = datetime.fromisoformat(at.removesuffix("Z") + "+00:00" if at.endswith("Z") else at).timestamp()
        # RawLog's legacy seconds/milliseconds contract must not reinterpret it.
        if not 0 <= stamp <= 1e11:
            raise ValueError("timestamp_out_of_range")
        time.localtime(round(stamp, 3))
    except (ValueError, OSError, OverflowError):
        raise AgentRequestError("invalid_at", field="at") from None
    return stamp


def source_tags(text, speaker, kind):
    tags = classify_source({"r": speaker, "t": text})
    if kind != "direct":
        tags.update(
            stance={"quote": "他引用", "forward": "他转述", "unknown": "来源不明"}[kind],
            support_scope="仅可作线索",
            stance_reason="接入来源为转述或不明确，不能确认是本人主张",
            classifier_version="agent-ingest-v1",
        )
        tags.pop("support_target", None)
    return {**tags, "ingest_source_kind": kind}


class AgentMemory:
    def __init__(self, directory, conversation, model=None):
        self.scope = conversation_scope(conversation)
        self.conversation = dict(conversation)
        self.lock = threading.RLock()
        self.store = Store(directory / "agent.sqlite3")
        self.raw = RawLog(str(directory / "agent-raw"))
        self.model_configured = model is not None
        self.pipeline = MemoryPipeline(self, model=model or self._provider_missing)

    @staticmethod
    def _provider_missing(*args):
        raise RuntimeError("extraction_provider_missing")

    def _check_scope(self, conversation):
        if conversation_scope(conversation) != self.scope:
            raise AgentRequestError("scope_forbidden", 403)

    def remember(self, data):
        require_fields(data, MESSAGE_FIELDS, ("message_id",))
        self._check_scope(data["conversation"])
        text = require_text(data, "text", 8000)
        speaker = data["speaker"]
        if not valid_speaker(speaker):
            raise AgentRequestError("invalid_speaker", field="speaker")
        stamp = parse_at(data["at"])
        kind = data["source_kind"]
        if not isinstance(kind, str) or kind not in SOURCE_KINDS:
            raise AgentRequestError("invalid_source_kind", field="source_kind")
        message_id = require_text(data, "message_id", 128) if "message_id" in data else uuid4().hex
        if not message_id.isprintable() or message_id != message_id.strip():
            raise AgentRequestError("invalid_message_id", field="message_id")
        tags = source_tags(text, speaker, kind)
        with self.lock:
            try:
                raw_id = self.raw.append(raw_session(self.scope), speaker, text, stamp, strict=True,
                                         extra={**tags, "message_id": message_id, "observed_at": data["at"]})
            except ValueError as exc:
                if str(exc) != "message_id_conflict":
                    raise
                raise AgentRequestError("message_id_conflict", 409) from None
            try:
                self.pipeline.notice({**self.scope, "raw_ids": [raw_id]})
                processed = self.pipeline.conn.execute(
                    "SELECT processed FROM memory_pipeline_raw WHERE raw_id=?", (raw_id,)).fetchone()[0]
            except (ValueError, sqlite3.Error, OSError, RuntimeError):
                raise AgentRequestError("raw_saved_binding_pending", 503,
                                        raw_id=raw_id, message_id=message_id) from None
        if processed == RETIRED_RAW:
            raise AgentRequestError("record_retired", 410)
        extraction = "raw_only" if speaker != "user" else (
            "processed" if processed == 2 else "queued" if self.model_configured else "awaiting_provider")
        return {"raw_id": raw_id, "message_id": message_id, "speaker": speaker, "source_kind": kind,
                "conversation": dict(self.conversation), "extraction": extraction, **tags}

    def judge(self, data):
        require_fields(data, ("conversation", "query"), ("exclude_raw_id",))
        self._check_scope(data["conversation"])
        query = require_text(data, "query", 1000)
        scope_query = {**self.scope, "text": query}
        if "exclude_raw_id" in data:
            scope_query["exclude_raw_id"] = require_text(data, "exclude_raw_id", 128)
        with self.lock:
            if not Path(self.raw.root).is_dir():
                raise unavailable()
            result = MemoryGates(self, strict_archive=True).one(scope_query)
        if result.get("ledger_write", {}).get("status") != "recorded":
            raise unavailable()
        if result.get("searched"):
            result["searched"] = {**result["searched"], "scope": dict(self.conversation)}
        evidence = result.get("evidence") or {}
        qualified = result["state"] == "found"
        if qualified and (not evidence.get("raw_id") or not evidence.get("quote")):
            raise unavailable()
        return {**result, "conversation": dict(self.conversation), "missing": result.get("missing"),
                "evidence_raw_id": evidence.get("raw_id"),
                "items": [evidence] if qualified else [],
                "allowed_raw_ids": [evidence["raw_id"]] if qualified else [],
                "citation_policy": dict(CITATION_POLICY)}

    def recall(self, data):
        require_fields(data, ("conversation", "query"), ("limit", "filters", "exclude_raw_id"))
        limit = data.get("limit", 3)
        if type(limit) is not int or not 1 <= limit <= 3:
            raise AgentRequestError("invalid_limit", field="limit")
        filters = data.get("filters", {})
        if not isinstance(filters, dict) or filters.keys() - set(SCOPE_FIELDS):
            raise AgentRequestError("unsupported_filter")
        if any(value != self.conversation[field] for field, value in filters.items()):
            raise AgentRequestError("scope_forbidden", 403)
        request = {key: data[key] for key in ("conversation", "query", "exclude_raw_id") if key in data}
        core_query = {**self.scope, "text": request["query"]}
        if "exclude_raw_id" in request:
            core_query["exclude_raw_id"] = request["exclude_raw_id"]
        with self.lock:
            result = self.judge(request)
            memories = TurnRecall(self).recall(core_query, explicit_lookup=True)
            if memories.get("error"):
                raise unavailable()
            speech = MemoryGates(self, strict_archive=True).two(core_query, include_all_speakers=True)
        utterances = [
            {**hit, "speaker": hit.get("send_role") or "未记录", "meta_only": True,
             "source_kind": hit.get("ingest_source_kind", "unknown")}
            for hit in speech["hits"][:limit]
        ]
        speaker_known = bool((result.get("evidence") or {}).get("send_role") or
                             any(hit["speaker"] != "未记录" for hit in utterances))
        return {
            **result, "hits": memories["hits"][:limit], "recall_error": memories.get("error"),
            "utterances": utterances,
            "speaker_status": "已记录" if speaker_known else "未记录",
        }

    def _originals(self):
        with self.lock, self.store._lock:
            index = [dict(row) for row in self.store.conn.execute(
                "SELECT * FROM memory_pipeline_raw WHERE scope=? AND session=? AND processed!=?",
                (scope_key(self.scope), raw_session(self.scope), RETIRED_RAW),
            )]
            originals = _assigned_records(self.raw.root, self.scope, index)
        if len(originals) != len(index) or not Path(self.raw.root).is_dir():
            raise unavailable()
        return originals

    def _original_item(self, record):
        return {"id": record["id"], "text": record["t"], "speaker": record["r"],
                "at": record.get("observed_at") or datetime.fromtimestamp(record["ts"], timezone.utc).isoformat(),
                "conversation": dict(self.conversation), "message_id": record.get("message_id"),
                "source_kind": record.get("ingest_source_kind", "unknown"),
                **{field: record[field] for field in ("stance", "support_scope", "stance_reason")}}

    def _read_item(self, identity, by_message_id):
        if not by_message_id and identity.startswith("memory:"):
            memory_id = validated_memory_id(identity[7:])
            row = self.store.get_memory(memory_id)
            if not row or row["status"] != "active" or not owns_memory(self, row, self.scope):
                return None
            return {"id": identity, "text": row["text"], "kind": row["kind"], "meta": row["meta"],
                    "at": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat(),
                    "conversation": dict(self.conversation)}
        originals = self._originals()
        record = originals.get(identity)
        if by_message_id:
            record = next((r for r in originals.values() if r.get("message_id") == identity), record)
        return self._original_item(record) if record else None

    def _read_args(self, data):
        require_fields(data, ("conversation", "id"), ("by_message_id",))
        self._check_scope(data["conversation"])
        by_message_id = data.get("by_message_id", False)
        if type(by_message_id) is not bool:
            raise AgentRequestError("invalid_by_message_id")
        return require_text(data, "id", 128), by_message_id

    def get(self, data):
        identity, by_key = self._read_args(data)
        with self.lock:
            try:
                return {"item": self._read_item(identity, by_key)}
            except DeleteError as exc:
                raise AgentRequestError(exc.error, exc.code) from None

    def delete(self, data):
        identity, by_key = self._read_args(data)
        with self.lock:
            try:
                if not by_key and identity.startswith("memory:"):
                    item = self._read_item(identity, by_key)
                    deleted = bool(item and self.store.delete_memory(
                        validated_memory_id(identity[7:]), "soft", actor="memory-agent", origin="agent_adapter"))
                else:
                    if by_key:
                        item = self._read_item(identity, by_key)
                        if not item:
                            return {"deleted": False}
                        identity = item["id"]
                    deleted = self.pipeline.retire_raw(self.scope, identity)
            except DeleteError as exc:
                raise AgentRequestError(exc.error, exc.code) from None
            except ValueError as exc:
                if str(exc) != "raw_extraction_in_progress":
                    raise
                raise AgentRequestError("raw_extraction_in_progress", 409) from None
        return {"deleted": deleted}

    def recent(self, data):
        require_fields(data, ("conversation",), ("limit",))
        self._check_scope(data["conversation"])
        limit = data.get("limit", 5)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise AgentRequestError("invalid_limit")
        rows = sorted(self._originals().values(), key=lambda item: (item["ts"], item["id"]))
        return {"items": [self._original_item(item) for item in rows[-limit:]]}
