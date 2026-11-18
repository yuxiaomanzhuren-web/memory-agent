"""The message contract and scoped memory operations shared by the adapters."""
from __future__ import annotations

from datetime import datetime
import re
import sqlite3
import threading
import time
from uuid import uuid4

from ..memory_evidence import raw_session, scope_from, valid_agent_id
from ..memory_gates import MemoryGates
from ..memory_pipeline import MemoryPipeline
from ..rawlog import RawLog, valid_speaker
from ..source_provenance import SOURCE_KINDS, classify_source
from ..store import Store
from ..turn_recall import TurnRecall


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
        extraction = "raw_only" if speaker != "user" else (
            "processed" if processed == 2 else "queued" if self.model_configured else "awaiting_provider")
        return {"raw_id": raw_id, "message_id": message_id, "speaker": speaker, "source_kind": kind,
                "conversation": dict(self.conversation), "extraction": extraction, **tags}

    def judge(self, data):
        require_fields(data, ("conversation", "query"))
        self._check_scope(data["conversation"])
        query = require_text(data, "query", 1000)
        with self.lock:
            result = MemoryGates(self).one({**self.scope, "text": query})
        if result.get("searched"):
            result["searched"] = {**result["searched"], "scope": dict(self.conversation)}
        evidence = result.get("evidence") or {}
        return {**result, "conversation": dict(self.conversation), "missing": result.get("missing"),
                "evidence_raw_id": evidence.get("raw_id")}

    def recall(self, data):
        require_fields(data, ("conversation", "query"), ("limit",))
        limit = data.get("limit", 3)
        if type(limit) is not int or not 1 <= limit <= 3:
            raise AgentRequestError("invalid_limit", field="limit")
        request = {key: data[key] for key in ("conversation", "query")}
        result = self.judge(request)
        core_query = {**self.scope, "text": request["query"]}
        with self.lock:
            memories = TurnRecall(self).recall(core_query, explicit_lookup=True)
            speech = MemoryGates(self).two(core_query, include_all_speakers=True)
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
