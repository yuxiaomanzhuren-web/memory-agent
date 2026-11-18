"""Explicit, scoped, reversible retirement; no scheduler or deletion path.

Only constructor schema initialization writes outside archive/restore. Callers
receive ValueError-compatible errors with HTTP ``code`` and ``error`` fields.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager

from .memory_evidence import owns_memory, scope_from, scope_key
from .operation_ledger import ensure_schema, event, restore_status, _restore_owned_row, _restore_code


class RetirementError(ValueError):
    def __init__(self, error, code=400):
        super().__init__(error)
        self.code, self.error = code, error


def _dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _meta(row):
    try:
        value = json.loads(row['meta'])
        if not isinstance(value, dict):
            return None
        if 'source_refs' in value and not isinstance(value['source_refs'], list):
            return None
        return value
    except (ValueError, TypeError):
        return None


def _fingerprint(row):
    return hashlib.sha256(_dump(row).encode('utf-8')).hexdigest()


def _daybook(meta):
    refs = meta.get('source_refs') or []
    return meta.get('source_kind') == 'daybook' or (
        isinstance(refs, list) and any(isinstance(ref, dict) and ref.get('source_kind') == 'daybook' for ref in refs))


class MemoryRetirement:
    def __init__(self, memory, clock=time.time):
        self.memory, self.store, self.clock = memory, memory.store, clock
        self.conn = getattr(self.store.conn, 'raw', self.store.conn)
        with self._tx():
            ensure_schema(self.conn)
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_retirement_history ON memory_retirement_audit(scope,id)')

    @contextmanager
    def _tx(self):
        with self.memory.lock, self.store._lock:
            if self.conn.in_transaction:
                raise RuntimeError('retirement requires its own short transaction')
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def _table(self, name):
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _row(self, mid):
        row = self.conn.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _number(data, key, default, upper=None):
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (upper is not None and value > upper):
            raise RetirementError('invalid_' + key)
        return value

    @staticmethod
    def _limit(data):
        value = data.get('limit', 100)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RetirementError('invalid_limit')
        return min(value, 100)

    def _protection(self, row, scope):
        meta = _meta(row)
        if meta is None:
            return 'invalid_metadata'
        if _daybook(meta):
            return 'daybook_projection'
        if meta.get('source_kind') in ('judgment', 'open_loop'):
            return 'judgment_or_review'
        if meta.get('source_kind') == 'self_review' and not meta.get('confirmed_by'):
            return 'pending_review'
        for flag in ('pinned', 'pin', 'protected', 'protect', 'do_not_archive', 'retirement_protected',
                     'unresolved', 'self_review_pending', 'review_pending', 'pending'):
            if meta.get(flag):
                return flag
        if meta.get('protection') or meta.get('protection_flags'):
            return 'explicit_protection'
        if row['kind'] in ('judgment', 'judge', 'open_loop', 'review', 'self_review'):
            return 'judgment_or_review'
        mid = row['id']
        if self._table('open_loops') and self.conn.execute(
                "SELECT 1 FROM open_loops WHERE source_memory_id=? AND status NOT IN ('closed','resolved','cancelled','done') LIMIT 1", (mid,)).fetchone():
            return 'unresolved_open_loop'
        if self._table('memory_pipeline_reviews') and self.conn.execute(
                "SELECT 1 FROM memory_pipeline_reviews WHERE memory_id=? AND status NOT IN ('rejected','confirmed','promoted','dismissed','resolved') LIMIT 1", (mid,)).fetchone():
            return 'pending_review'
        if self._table('pending_thoughts') and self.conn.execute(
                "SELECT 1 FROM pending_thoughts WHERE bot_id=? AND persona=? AND conv_key=? AND (state IN ('reserved','unknown') OR (state IN ('ready','dormant','deferred') AND expires_at>?)) LIMIT 1",
                (scope['bot_id'], scope['persona'], scope['conv_key'], self.clock())).fetchone():
            # This queue has no memory-id attribution; fail closed within scope.
            return 'pending_scope_dependency'
        if self._table('memory_links'):
            removed = " AND NOT EXISTS (SELECT 1 FROM memory_pipeline_link_meta p WHERE p.link_id=l.id AND p.state='removed')" if self._table('memory_pipeline_link_meta') else ''
            if self.conn.execute("SELECT 1 FROM memory_links l WHERE ((a_kind='memory' AND a_id=?) OR (b_kind='memory' AND b_id=?))" + removed + ' LIMIT 1', (mid, mid)).fetchone():
                return 'live_link_dependency'
        return None

    def preview(self, data):
        scope = scope_from(data)
        days = self._number(data, 'min_age_days', 30)
        importance = self._number(data, 'max_importance', .25, 1)
        limit, now = self._limit(data), self.clock()
        items, excluded = [], Counter()
        with self.memory.lock, self.store._lock:
            for raw in self.conn.execute('SELECT * FROM memories ORDER BY created_at,id').fetchall():
                row = dict(raw)
                if not owns_memory(self.memory, row, scope):
                    excluded['unowned'] += 1
                    continue
                reason = ('not_active' if row['status'] != 'active' else self._protection(row, scope))
                if not reason and row['created_at'] > now - days * 86400:
                    reason = 'too_recent'
                if not reason and row['importance'] > importance:
                    reason = 'importance'
                if reason:
                    excluded[reason] += 1
                elif len(items) < limit:
                    items.append({'id': row['id'], 'fingerprint': _fingerprint(row),
                                  'text': row['text'][:200], 'kind': row['kind'],
                                  'reason': 'old_low_importance', 'created_at': row['created_at'],
                                  'importance': row['importance']})
                else:
                    excluded['limit'] += 1
        return {'items': items, 'exclusions': dict(excluded), 'excluded_count': sum(excluded.values()),
                'policy': {'min_age_days': days, 'max_importance': importance}}

    @staticmethod
    def _text(data, name, limit):
        value = data.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise RetirementError('invalid_' + name)
        return value

    def _mutate(self, data, operation):
        scope = scope_from(data)
        key = scope_key(scope)
        request_id = self._text(data, 'request_id', 200)
        reason, operator = self._text(data, 'reason', 1000), self._text(data, 'operator', 200)
        items = data.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise RetirementError('invalid_items')
        normalized, seen = [], set()
        for item in items:
            mid = item.get('id') if isinstance(item, dict) else None
            if isinstance(mid, bool) or not isinstance(mid, int) or mid < 1 or mid in seen:
                raise RetirementError('invalid_id')
            seen.add(mid)
            value = {'id': mid}
            if operation == 'archive':
                value['fingerprint'] = self._text(item, 'fingerprint', 64)
            normalized.append(value)
        strict_restore = operation == 'restore' and any(data.get(k) is not None for k in ('expected_audit_id','expected_request_id'))
        payload = _dump({'scope': scope, 'operation': operation, 'data': data})
        with self._tx():
            prior = self.conn.execute('SELECT payload,receipt FROM memory_retirement_receipts WHERE scope=? AND request_id=?', (key, request_id)).fetchone()
            if prior:
                if prior['payload'] != payload:
                    raise RetirementError('request_id_conflict', 409)
                if strict_restore and any(_restore_owned_row(self.memory,scope,item['id']) is None for item in normalized):
                    raise RetirementError('memory_not_owned',403)
                return json.loads(prior['receipt'])
            checked, sources = [], {}
            for item in normalized:
                row = self._row(item['id'])
                if row is None or not owns_memory(self.memory, row, scope):
                    raise RetirementError('memory_not_owned', 403)
                if operation == 'archive':
                    protection = self._protection(row, scope)
                    if row['status'] != 'active' or item['fingerprint'] != _fingerprint(row) or protection:
                        raise RetirementError('archive_rejected:' + (protection or 'stale_fingerprint_or_status'), 409)
                else:
                    meta = _meta(row)
                    if meta is None or _daybook(meta):
                        raise RetirementError('daybook_or_invalid_projection_restore_denied', 409)
                    if strict_restore:
                        status = restore_status(self.memory, scope, row['id'], expected_audit_id=data.get('expected_audit_id'),
                                                expected_request_id=data.get('expected_request_id'))
                        if not status['allowed'] or status['route'] != 'archive':
                            raise RetirementError(status['reason_code'] if not status['allowed'] else 'not_archive_source',
                                                  _restore_code(status['reason_code']))
                        sources[row['id']] = status
                    last = self.conn.execute('SELECT scope,operation,fingerprint FROM memory_retirement_audit WHERE memory_id=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
                    if row['status'] != 'archived' or not last or last['scope'] != key or last['operation'] != 'archive' or last['fingerprint'] != _fingerprint(row):
                        raise RetirementError('not_current_retirement_archive', 409)
                checked.append(row)
            now = self.clock()
            actor = data.get('actor_kind', data.get('actor', 'unknown'))
            events = []
            for row in checked:
                self.conn.execute('UPDATE memories SET status=?,archived_at=? WHERE id=?',
                                  ('archived' if operation == 'archive' else 'active', now if operation == 'archive' else 0, row['id']))
                audit = self.conn.execute('INSERT INTO memory_retirement_audit(scope,request_id,operation,memory_id,fingerprint,reason,operator,created_at) VALUES(?,?,?,?,?,?,?,?)',
                                          (key, request_id, operation, row['id'], _fingerprint(self._row(row['id'])), reason, operator, now))
                pointers = ({'details': {'source_audit_id': sources[row['id']]['audit_id'],
                                         'source_request_id': sources[row['id']]['request_id'], 'restored_status': 'active'}}
                            if row['id'] in sources else {})
                events.append(event(operation, {'type': 'memory', 'id': row['id']}, 'explicit_retirement', actor,
                                    True, scope, now, operation=operation, reason=reason, operator=operator,
                                    request_id=request_id, audit_id=audit.lastrowid, memory_id=row['id'],
                                    ids=[row['id']], created_at=now, **pointers))
            receipt = {'ok': True, 'request_id': request_id, 'operation': operation,
                       'ids': [row['id'] for row in checked], 'count': len(checked), 'created_at': now,
                       'reversible': True, 'reason': reason, 'actor': actor, 'scope': scope,
                       'scope_status': 'bound', 'at': now, 'at_text': events[0]['at_text'], 'items': events}
            self.conn.execute('INSERT INTO memory_retirement_receipts VALUES(?,?,?,?)', (key, request_id, payload, _dump(receipt)))
            return receipt

    def archive(self, data):
        return self._mutate(data, 'archive')

    def restore(self, data):
        try:
            return self._mutate(data, 'restore')
        except (sqlite3.Error,RuntimeError) as error:
            if not any(data.get(k) is not None for k in ('expected_audit_id','expected_request_id')):raise
            if isinstance(error,sqlite3.Error):raise RetirementError('restore_unavailable',503) from None
            raise RetirementError('restore_transaction_conflict',409) from None

    def history(self, data):
        key, limit = scope_key(scope_from(data)), self._limit(data)
        with self.memory.lock, self.store._lock:
            return {'items': [dict(row) for row in self.conn.execute(
                'SELECT id,request_id,operation,memory_id,reason,operator,created_at FROM memory_retirement_audit WHERE scope=? ORDER BY id DESC LIMIT ?', (key, limit)).fetchall()]}
