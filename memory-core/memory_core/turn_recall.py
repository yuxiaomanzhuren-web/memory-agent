"""Verified turn recall and scoped surfacing; explicit lookups stay read-only."""
from __future__ import annotations
from .source_provenance import aggregate, readable, rank, constrain

import hashlib
import math
import re
import sqlite3
import time
from contextlib import closing, nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .memory_evidence import (
    _decode_line, _metadata, _timestamp, _verify_record, owns_memory,
    raw_records, raw_session, scope_from, scope_key,
)

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
MAX_QUOTE_CHARS = 200
MAX_TEXT_CHARS = 220
MAX_CONTEXT_CHARS = 1400
REPEAT_SECONDS = 2 * 3600
COOL_SECONDS = 7 * 86400
_EXPLICIT = re.compile(r"还记得|记不记得|记得|原话|原句|说过|聊过|提过|讲过|之前|上次|那天|什么时候|什么时间|\b(?:remember|recall)\b", re.I)
_DATES = re.compile(r"(?<![0-9])\d{4}-\d{2}-\d{2}(?![0-9])|今天|昨天|\btoday\b|\byesterday\b", re.I)
_QUESTIONS = (
    "你还记得", "还记得", "什么时候", "什么时间", "为什么", "说过什么", "聊了什么",
    "做了什么", "发生什么", "聊过什么", "告诉过", "记不记得", "有没有", "哪些", "什么",
    "怎么", "怎么样", "那个", "那天", "之前", "上次", "记得", "聊了", "聊过", "说过",
    "跟你", "跟我", "对你", "对我", "告诉你", "告诉我", "提过什么", "讲了什么",
)
_STOP_CHARS = set("的了着过吗呢吧啊呀哦嘛嗯是我你他她它这那说会要不得把被给和与都也就还在从到对很太更最但又再才让么")
_STOP_WORDS = set("a an the i you we he she it they me my your our their is are was were be been do did does have has had what when where why how who which about remember recall discuss discussed say said talk talked tell told please can could would should of for to with and or in on at that this then".split())


def _query(text):
    today = datetime.fromtimestamp(time.time(), SHANGHAI).date()
    days = set()
    for match in _DATES.finditer(text):
        token = match.group().lower()
        if token in ("今天", "today"):
            days.add(today)
        elif token in ("昨天", "yesterday"):
            days.add(today - timedelta(days=1))
        else:
            days.add(date.fromisoformat(token))
    topic = _DATES.sub(" ", text).lower()
    for phrase in sorted(_QUESTIONS, key=len, reverse=True):
        topic = topic.replace(phrase, " ")
    return days, _tokens(topic), _speaker(text)


def _speaker(text):
    text = re.sub(r"\s+", "", _DATES.sub(" ", text))
    # Speaker intent must describe a past utterance at the start of the query.
    # A quotation containing "你可以直接告诉我" is not an assistant filter.
    text = re.sub(r"^(?:请问|还记得|你还记得)", "", text)
    for first, second, role in (("我", "你", "user"), ("你", "我", "assistant")):
        if re.match(first + r"(?!们)(?:曾经|之前|刚才)?(?:跟|对|向|和|给)" + second + r"(?:说|讲|提|聊|告诉)", text):
            return role
    match = re.match(r"(我|你)(?!们)(?:曾经|之前|刚才)?(?:说过|提过|告诉过|讲过|说了|讲了)", text)
    if match:
        return "user" if match[1] == "我" else "assistant"
    # English intent is checked against the original spacing.
    return None


def _tokens(text):
    tokens = {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 2 and w not in _STOP_WORDS}
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.update(run[i:i + 2] for i in range(len(run) - 1)
                      if not any(ch in _STOP_CHARS for ch in run[i:i + 2]))
    return tokens


def _overlap(tokens, text):
    matched = sorted(tokens & _tokens(text))
    # A lone matching fragment in a longer question is too weak to inject.
    minimum = 1 if len(tokens) == 1 else max(2, math.ceil(len(tokens) * 0.5))
    return matched if len(matched) >= minimum else []


def _on_day(ts, days):
    return datetime.fromtimestamp(_timestamp(ts), SHANGHAI).date() in days


def _why(matched, ref):
    parts = []
    if matched:
        parts.append("原话匹配：" + "、".join(matched[:8]))
    parts.append("原话日期：" + datetime.fromtimestamp(_timestamp(ref["raw_ts"]), SHANGHAI).date().isoformat())
    parts.append("说话者：" + {"user": "用户", "assistant": "助手"}.get(ref["role"], ref["role"]))
    return "；".join(parts)


def _excerpt(text, matched, limit):
    if len(text) <= limit:
        return text
    positions = [text.lower().find(term) for term in matched]
    positions = [pos for pos in positions if pos >= 0]
    start = max(0, min(positions) - 40) if positions else 0
    return text[start:start + limit]


def _best_source(refs, tokens, days, speaker):
    """Return one bounded, relevant original excerpt with its verified location."""
    choices = []
    for ref in refs:
        if (speaker and ref["role"] != speaker) or (days and not _on_day(ref["raw_ts"], days)):
            continue
        matched = _overlap(tokens, ref["quote"]) if tokens else []
        if tokens and not matched:
            continue
        quote = _excerpt(ref["quote"], matched, MAX_QUOTE_CHARS)
        matched = _overlap(tokens, quote) if tokens else []
        if tokens and not matched:
            continue
        choices.append(({**ref, "quote": quote}, matched))
    choices.sort(key=lambda pair: (-len(pair[1]), -_timestamp(pair[0]["raw_ts"]), pair[0]["raw_id"]))
    return choices[0] if choices else (None, [])


def _rows(conn, sql, args=()):
    cursor = conn.execute(sql, args)
    names = [col[0] for col in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _assigned_records(root, scope, index):
    """Seek only explicitly assigned rows; never scan unassigned raw history."""
    root = Path(root).resolve()
    from .source_provenance import indexed_tags
    records = {}
    for row in index:
        try:
            if row["scope"] != scope_key(scope) or row["session"] != raw_session(scope):
                continue
            path = (root / row["file"]).resolve()
            if not path.is_relative_to(root) or path.suffix != ".jsonl":
                continue
            offset, size = row["offset"], row["size"]
            if type(offset) is not int or type(size) is not int or offset < 0 or size <= 0:
                continue
            with path.open("rb") as stream:
                if offset:
                    stream.seek(offset - 1)
                    if stream.read(1) != b"\n":
                        continue
                stream.seek(offset)
                blob = stream.read(size)
            if len(blob) != size or hashlib.sha256(blob).hexdigest() != row["sha256"]:
                continue
            rec = _decode_line(blob)
            if (rec["id"] != row["raw_id"] or rec["s"] != raw_session(scope)
                    or rec["r"] != row["role"] or _timestamp(rec["ts"]) != _timestamp(row["ts"])):
                continue
            records[rec["id"]] = {
                **rec, "file": path.relative_to(root).as_posix(), "offset": offset,
                "size": size, "sha256": row["sha256"], **indexed_tags(row, rec),
            }
        except (OSError, ValueError, TypeError, OverflowError):
            continue
    return records


class TurnRecall:
    def __init__(self, memory):
        self.memory = memory

    @staticmethod
    def _empty(why, error=None):
        out = {"hits": [], "block": "", "why": why}
        if error:
            out["error"] = error
        return out

    def recall(self, data, *, select_hits=None, explicit_lookup=False) -> dict:
        trace = [] if isinstance(data, dict) and data.get('diagnostics') is True else None
        out = self._request_recall(data, select_hits=select_hits, trace=trace, explicit_lookup=explicit_lookup)
        if trace is not None:
            hit_attempt = next((a for a in reversed(trace) if a['outcome'] == 'hit'), None)
            miss = next((name for name in ('final_merge', 'cooldown', 'invalid_candidates', 'query_gate',
                                           'no_verified_sources', 'no_owned_memory', 'literal_miss')
                         if any(a['outcome'] == name for a in trace)), 'no_attempts')
            out['diagnostics'] = dict(wired=True, version=1,
                selected_route=hit_attempt['route'] if hit_attempt else 'none',
                surface_entered=any(a.get('surface_entered') for a in trace),
                outcome=out.get('error') or ('hit' if out['hits'] else miss),
                attempts=trace)
        return out

    def _attempt(self, scope, days, tokens, speaker, *, trace=None, **options):
        diag = dict(route='literal' if options.get('literal') is not None else
                    'explicit' if options.get('explicit') or days or speaker else 'surface',
                    owned_rows=0, verified_rows=0, invalid_rows=0, no_evidence_rows=0,
                    query_gate_rejected=0, candidate_ids=[], surface_entered=False,
                    cooldown_blocked=0, limit_filtered=0, merge_filtered=0)
        if trace is not None:
            trace.append(diag)
        try:
            out = self._recall(scope, days, tokens, speaker,
                               diagnostics=diag if trace is not None else None, **options)
        except Exception:
            diag['outcome'] = 'unavailable'
            raise
        diag['returned'] = len(out['hits'])
        if out['hits']:
            diag['outcome'] = 'hit'
        elif diag['route'] == 'literal':
            diag['outcome'] = 'literal_miss'
        elif not diag['owned_rows']:
            diag['outcome'] = 'no_owned_memory'
        elif not diag['verified_rows']:
            diag['outcome'] = 'no_verified_sources'
        elif not diag['candidate_ids']:
            diag['outcome'] = 'invalid_candidates' if diag['invalid_rows'] else 'query_gate'
        elif diag['cooldown_blocked'] == len(diag['candidate_ids']):
            diag['outcome'] = 'cooldown'
        elif diag['merge_filtered']:
            diag['outcome'] = 'final_merge'
        else:
            diag['outcome'] = 'no_match'
        return out

    def _request_recall(self, data, *, select_hits=None, trace=None, explicit_lookup=False) -> dict:
        try:
            scope = scope_from(data)
        except ValueError:
            return self._empty("范围无效", "invalid_scope")
        text = data.get("text", data.get("query", ""))
        if not isinstance(text, str) or not text.strip():
            return self._empty("查询为空")
        from .memory_temporal import wants_history
        history = wants_history(data)
        try:
            # Exact verified originals may themselves contain intent words or
            # start with "我说过". Only date queries retain their date filter.
            if len(text.strip()) >= 12 and not _DATES.search(text):
                literal = self._attempt(scope, set(), _tokens(text), None, literal=text.strip(), trace=trace, include_history=history)
                if literal['hits']:
                    return literal
            days, tokens, speaker = _query(text)
            if not days and not tokens:
                return self._empty("没有可匹配的内容")
            # Only the explicit verification entry point sets this trusted
            # keyword. A JSON field cannot disable normal passive cooldown.
            explicit = explicit_lookup or bool(days or speaker or _EXPLICIT.search(text))
            result = self._attempt(scope, days, tokens, speaker, explicit=explicit, select_hits=select_hits, trace=trace, include_history=history)
            if result['hits'] or result.get('error'):
                return result
            from .synonyms import variants

            for alternate in variants(text, limit=1):
                alt_days, alt_tokens, alt_speaker = _query(alternate)
                expanded = self._attempt(scope, alt_days, alt_tokens, alt_speaker,
                                         explicit=explicit, select_hits=select_hits, trace=trace, include_history=history)
                if trace is not None:
                    trace[-1]['query_variant'] = alternate
                if expanded['hits']:
                    for hit in expanded['hits']:
                        hit['why'] = '同义词回查；' + hit['why']
                        hit['why_now'] = hit['why']
                    expanded['block'] = '同义词回查；' + expanded['block']
                    expanded['query_variant'] = alternate
                    return expanded
            return result
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, sqlite3.Error):
            return self._empty("召回暂不可用", "recall_unavailable")

    def _surface(self, scope, candidates, select_hits=None, diagnostics=None):
        """Atomically select and log on a separate connection, never commit callers.

        The server's trusted selector accounts for diary hits before logging.
        Selection and logging share the guard transaction. No cached DB handles.
        """
        if diagnostics is not None:
            diagnostics['surface_entered'] = True
        if not candidates:
            return []
        now = time.time()
        key = scope_key(scope)
        with closing(sqlite3.connect(str(self.memory.store.path), timeout=.1)) as conn:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('''CREATE TABLE IF NOT EXISTS memory_surface_log(
                    id INTEGER PRIMARY KEY,scope TEXT NOT NULL,memory_id INTEGER NOT NULL,
                    at REAL NOT NULL,why_now TEXT NOT NULL)''')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_surface_log ON memory_surface_log(scope,memory_id,at)')
                last_at = dict(conn.execute(
                    'SELECT memory_id,MAX(at) FROM memory_surface_log WHERE scope=? GROUP BY memory_id', (key,)))
                eligible = []
                for hit in candidates:
                    last = _timestamp(last_at.get(hit['id'], 0))
                    if last and now-last < REPEAT_SECONDS:
                        if diagnostics is not None:
                            diagnostics['cooldown_blocked'] += 1
                        continue
                    # A new memory has not accumulated a long unmentioned span.
                    since = max(hit.pop('_created'), last)
                    age = max(0., now-since)
                    hit['score'] = (.5 * min(1., age/COOL_SECONDS)
                                    + .3 * hit['importance'] + .2 * hit['score'])
                    hit['why_now'] = (hit['why'] + f'；距创建或上次浮起 {age/86400:.1f} 天'
                                      f'；重要度 {hit["importance"]:.2f}')
                    hit['why'] = hit['why_now']
                    eligible.append(hit)
                eligible.sort(key=lambda hit: (-hit['score'], -hit['importance'], str(hit['id'])))
                hits = eligible[:3]
                if diagnostics is not None:
                    diagnostics['limit_filtered'] = max(0, len(eligible) - len(hits))
                if select_hits is not None:
                    before_merge = len(hits)
                    selected = {(hit['source_kind'], hit['id']) for hit in select_hits(hits)}
                    hits = [hit for hit in hits if (hit['source_kind'], hit['id']) in selected]
                    if diagnostics is not None:
                        diagnostics['merge_filtered'] = before_merge - len(hits)
                conn.executemany('INSERT INTO memory_surface_log(scope,memory_id,at,why_now) VALUES(?,?,?,?)',
                                 [(key, hit['id'], now, hit['why_now']) for hit in hits])
                return hits

    def _recall(self, scope, days, tokens, speaker, literal=None, explicit=False, select_hits=None, diagnostics=None, include_history=False):
        memory = self.memory
        lock = getattr(memory.store, "_lock", None)
        with lock if lock is not None else nullcontext():
            rows = _rows(memory.store.conn, "SELECT * FROM memories WHERE status='active' ORDER BY id")
            owned = [row for row in rows if owns_memory(memory, row, scope)]
            if diagnostics is not None:
                diagnostics['owned_rows'] = len(owned)
            index = []
            try:
                index = _rows(memory.store.conn,
                              "SELECT * FROM memory_pipeline_raw WHERE scope=? AND session=? ORDER BY raw_id",
                              (scope_key(scope), raw_session(scope)))
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
        if not days and owned and index and literal is None:
            # The general path may scan to detect ambiguous duplicate IDs. Date
            # recall reads only the indexed, explicitly assigned byte ranges.
            raw_records(memory.raw.root, raw_session(scope))
        records = _assigned_records(memory.raw.root, scope, index)
        from .memory_temporal import filter_memories, hidden_raw_ids
        owned = filter_memories(memory.store.conn, owned, include_history=include_history)
        if not include_history:
            hidden = hidden_raw_ids(memory.store.conn)
            records = {rid:rec for rid,rec in records.items() if rid not in hidden}
        candidates = []
        for row in owned:
            meta = _metadata(row)
            refs = meta.get("source_refs")
            if refs is None and all(key in meta for key in ("raw_id", "raw_ts", "quote")):
                refs = [{key: meta[key] for key in ("raw_id", "raw_ts", "quote")}]
            if not isinstance(refs, list) or not refs:
                if diagnostics is not None:
                    diagnostics['no_evidence_rows'] += 1
                continue
            try:
                verified = [constrain(_verify_record(scope, ref, records),meta,stage='recall_entry') for ref in refs]
                if diagnostics is not None:
                    diagnostics['verified_rows'] += 1
                if literal is not None:
                    exact = [ref for ref in verified if literal == ref['quote'].strip()
                             or literal == records[ref['raw_id']]['t'].strip()]
                    if not exact:
                        continue
                    ref = dict(exact[0])
                    ref['quote'] = ref['quote'][:MAX_QUOTE_CHARS]
                    matched = sorted(tokens & _tokens(ref['quote']))
                else:
                    ref, matched = _best_source(verified, tokens, days, speaker)
                if ref is None or rank(ref) == 0:
                    if diagnostics is not None:
                        diagnostics['query_gate_rejected'] += 1
                    continue
                if meta.get("source_role") and meta["source_role"] != ref["role"]:
                    if diagnostics is not None:
                        diagnostics['invalid_rows'] += 1
                    continue
                importance = min(1., max(0., _timestamp(row["importance"])))
                created = _timestamp(row['created_at'])
            except (ValueError, TypeError, OverflowError):
                if diagnostics is not None:
                    diagnostics['invalid_rows'] += 1
                continue
            candidates.append({
                "id": row["id"], "text": _excerpt(row["text"], matched, MAX_TEXT_CHARS), "kind": row["kind"],
                "importance": importance, "why": ('原句逐字命中；' if literal is not None else '') + _why(matched, ref), "source_refs": [ref],
                "source_kind": meta.get("source_kind") or "memory", "source_role": ref["role"], **aggregate([ref], stage="recall"),
                "score": (1.0 if literal is not None else len(matched) / max(1, len(tokens))) * row.get('fact_version',{}).get('weight',1.0),
                "fact_version": row.get('fact_version'),
                "_created": created,
            })
        if days:
            used = {ref["raw_id"] for hit in candidates for ref in hit["source_refs"]}
            for rec in records.values():
                if rec["id"] in used or not _on_day(rec["ts"], days):
                    continue
                ref = _verify_record(scope, {"raw_id": rec["id"], "quote": rec["t"]}, records, from_source=True)
                ref, matched = _best_source([ref], tokens, days, speaker)
                if ref is None or rank(ref) == 0:
                    continue
                candidates.append({
                    "id": rec["id"], "raw_id": rec["id"], "text": ref["quote"], "kind": "raw",
                    "importance": 0.0, "why": _why(matched, ref), "source_refs": [ref],
                    "source_kind": "raw", "source_role": ref["role"], **aggregate([ref], stage="recall"), "score": len(matched) / max(1, len(tokens)),
                })
        candidates.sort(key=lambda hit: (
            -hit["score"], -hit["importance"],
            -max(_timestamp(ref["raw_ts"]) for ref in hit["source_refs"]),
            str(hit["id"]),
        ))
        if diagnostics is not None:
            diagnostics['candidate_ids'] = [hit['id'] for hit in candidates]
        if not (explicit or literal is not None or days or speaker):
            hits = self._surface(scope, candidates, select_hits=select_hits, diagnostics=diagnostics)
        else:
            hits = candidates[:3]
            if diagnostics is not None:
                diagnostics['limit_filtered'] = max(0, len(candidates) - len(hits))
        for hit in hits:
            hit.pop('_created', None)
            hit.setdefault('why_now', hit['why'])
        if not hits:
            return self._empty("没有相关且已核验的原话")
        block = "\n".join(
            "- [" + readable(hit) + "] " + ("[助手自我回顾] " if hit["source_kind"] == "self_review" else "")
            + (f"[版本 {hit['fact_version']['stance_tag']}，取代它的边={hit['fact_version']['superseded_by']}] "
               if include_history and hit.get('fact_version') else '')
            + hit["text"] + "（" + hit["why"] + "）" for hit in hits
        )
        quote_chars = sum(len(ref["quote"]) for hit in hits for ref in hit["source_refs"])
        block = block[:MAX_CONTEXT_CHARS - quote_chars]
        return {"hits": hits, "block": block, "why": "已按原话内容、时间和说话者核验"}
