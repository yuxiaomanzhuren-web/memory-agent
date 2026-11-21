"""Read-only scope ownership and byte-verifiable raw evidence.

Raw locations are relative to the archive root. Offsets, sizes and SHA-256 cover
the complete original UTF-8 line, including its newline. Ambiguous IDs fail
closed; identical copies use the first path in lexical order.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from .source_provenance import normalize, rank
from pathlib import Path


def valid_agent_id(value) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 120 and value == value.strip() and value.isprintable()


def agent_conversation(scope) -> dict | None:
    scope = scope_from(scope)
    if not scope["bot_id"].startswith("agent:"):
        return None
    return {
        "source_id": scope["bot_id"].removeprefix("agent:"),
        "owner_id": scope["persona"],
        "conversation_id": scope["conv_key"].removeprefix("private:"),
    }


def scope_from(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("invalid_scope")
    bot = data.get("bot_id")
    if isinstance(bot, bool) or not isinstance(bot, (str, int)):
        raise ValueError("invalid_bot_id")
    persona = data.get("persona")
    conv = data.get("conv_key")
    if isinstance(bot, str) and bot.startswith("agent:"):
        if not valid_agent_id(bot.removeprefix("agent:")):
            raise ValueError("invalid_bot_id")
        if not valid_agent_id(persona):
            raise ValueError("invalid_persona")
        if not isinstance(conv, str) or not conv.startswith("private:") or not valid_agent_id(conv.removeprefix("private:")):
            raise ValueError("invalid_conv_key")
        return {"bot_id": bot, "persona": persona, "conv_key": conv}
    bot = str(bot).strip()
    if not re.fullmatch(r"[1-9][0-9]*", bot):
        raise ValueError("invalid_bot_id")
    if not isinstance(persona, str) or not persona.strip() or len(persona) > 120:
        raise ValueError("invalid_persona")
    if not isinstance(conv, str) or not re.fullmatch(r"(?:private|group):[1-9][0-9]*", conv):
        raise ValueError("invalid_conv_key")
    return {"bot_id": bot, "persona": persona, "conv_key": conv}


def scope_key(scope) -> str:
    return json.dumps(scope_from(scope), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def raw_session(scope) -> str:
    scope = scope_from(scope)
    if scope["bot_id"].startswith("agent:"):
        # JSON retains all three opaque IDs without delimiter collisions.
        return "agent:" + scope_key(scope)
    return f"{scope['bot_id']}|{scope['conv_key']}"


def _timestamp(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid_raw_ts")
    ts = float(value)
    if not math.isfinite(ts):
        raise ValueError("invalid_raw_ts")
    return ts


def _decode_line(blob: bytes) -> dict:
    if not blob.endswith(b"\n"):
        raise ValueError("incomplete_raw_line")
    rec = json.loads(blob.decode("utf-8"))
    if not isinstance(rec, dict):
        raise ValueError("invalid_raw_record")
    if any(not isinstance(rec.get(k), str) or not rec[k] for k in ("id", "s", "r", "t")):
        raise ValueError("invalid_raw_record")
    _timestamp(rec.get("ts"))
    return rec


def raw_records(root, session=None) -> dict:
    """Read every JSONL shard without repairing it or creating an index.

    Malformed lines and an uncommitted tail are ignored. Different records with
    the same ID raise ValueError, even when one is outside the session filter.
    """
    root = Path(root).resolve()
    records, originals = {}, {}
    for path in sorted(root.rglob("*.jsonl")):
        if not path.resolve().is_relative_to(root):
            raise ValueError("raw_file_outside_root")
        with path.open("rb") as stream:
            while True:
                offset = stream.tell()
                blob = stream.readline()
                if not blob:
                    break
                try:
                    rec = _decode_line(blob)
                except (ValueError, UnicodeError, OverflowError):
                    continue
                rid = rec["id"]
                digest = hashlib.sha256(blob).hexdigest()
                if rid in originals:
                    if originals[rid] != digest:
                        raise ValueError(f"duplicate raw id conflict: {rid}")
                    continue
                originals[rid] = digest
                if session is None or rec["s"] == session:
                    records[rid] = {
                        **rec, "file": path.relative_to(root).as_posix(),
                        "offset": offset, "size": len(blob),
                        "sha256": digest,
                    }
    return records


def _verify_record(scope, ref, records, *, from_source=False, source_tags=None) -> dict:
    if not isinstance(ref, dict):
        raise ValueError("invalid_source_ref")
    rid, quote = ref.get("raw_id"), ref.get("quote")
    if not isinstance(rid, str) or not isinstance(quote, str) or not quote.strip():
        raise ValueError("invalid_source_ref")
    rec = records.get(rid)
    if rec is None or rec["s"] != raw_session(scope):
        raise ValueError("raw_source_not_in_scope")
    if quote not in rec["t"]:
        raise ValueError("raw_quote_mismatch")
    if "raw_ts" in ref and _timestamp(ref["raw_ts"]) != _timestamp(rec["ts"]):
        raise ValueError("raw_ts_mismatch")
    verified = {
        "raw_id": rec["id"], "raw_ts": rec["ts"], "quote": quote, "role": rec["r"],
        "raw_file": rec["file"], "raw_offset": rec["offset"], "raw_sha256": rec["sha256"],
    }
    for key in ("role", "raw_file", "raw_offset", "raw_sha256"):
        if key in ref and ref[key] != verified[key]:
            raise ValueError(f"{key}_mismatch")
    authority=normalize({**(source_tags if source_tags is not None else rec),'role':rec['r']},raw_id=rec['id'],stage='source_verify')
    tags=authority if from_source else normalize({**ref,'role':rec['r']},raw_id=rec['id'],stage='ref_verify')
    if rank(authority)<rank(tags):tags=authority
    verified.update(tags)
    if "ingest_source_kind" in authority:
        verified["ingest_source_kind"] = authority["ingest_source_kind"]
    return verified


def verify_ref(root, scope, ref, *, from_source=False, source_tags=None) -> dict:
    scope = scope_from(scope)
    return _verify_record(scope, ref, raw_records(root, raw_session(scope)), from_source=from_source, source_tags=source_tags)


def _metadata(row) -> dict:
    meta = dict(row).get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return {}
    return meta if isinstance(meta, dict) else {}


RETIRED_RAW = 3  # Durable retirement in memory_pipeline_raw.processed.


def owns_memory(memory, row, scope) -> bool:
    """Explicit metadata or adoption only; legacy source strings confer no rights.

    A conflicting metadata owner cannot be overridden by an adoption sidecar.
    Missing sidecar tables on older installations simply mean no adoption.
    """
    scope = scope_from(scope)
    meta = _metadata(row)
    if "pipeline_scope" in meta:
        return meta["pipeline_scope"] == scope or meta["pipeline_scope"] == scope_key(scope)
    try:
        owner = memory.store.conn.execute(
            "SELECT scope FROM memory_pipeline_owners WHERE memory_id=?", (dict(row)["id"],)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return False
    return owner is not None and owner[0] == scope_key(scope)
