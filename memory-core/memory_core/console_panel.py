"""Isolated scoped inventory/diagnostic reads; the outer boundary authenticates Access."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import time

from .memory_evidence import owns_memory, raw_session, scope_key
from .operation_ledger import memory_scope
from .scope_auth import ScopeError, current
from .source_provenance import CLUE, DIRECT, STANCES


PREFIX = '/api/console/panel/'
ROUTES = (PREFIX + 'memories', PREFIX + 'memory', PREFIX + 'logs/tail')
MAX_INVENTORY = 10000
MAX_RELATED = 1000
MAX_LOG_BYTES = 256 * 1024
MAX_LOG_SCAN_LINES = 2000
MAX_LOG_SECONDS = .1
MAX_LOG_LINE_BYTES = 16384
CURSOR_SECONDS = 3600
_CURSOR_KEY = secrets.token_bytes(32)
_MEMORY_COLUMNS = 'id,kind,text,status,importance,created_at,archived_at,meta'
_SCOPE_FIELDS = {'bot_id', 'persona', 'conv_key'}
_RELATIONS = {'同一件事', '同话题', '后来变了', '前后矛盾', '因为', '纠正了', '先后发生'}
_LINK_TIMES = ('t_valid', 't_invalid', 't_created', 't_expired')
_SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
_LOG_ENUMS = {
    'event': {'scope_recall', 'gate1', 'gate1_trigger', 'gate2', 'gate3', 'output_verify',
              'source_verify', 'extraction', 'archive', 'restore', 'request'},
    'status': {'ok', 'error', 'failed', 'pending', 'completed', 'skipped', 'unknown'},
    'state': {'found', 'suspected', 'absent', 'not_checked', 'unknown'},
    'branch': {'request', 'fts', 'vectors', 'pin', 'graph', 'synonyms', 'literal_fts',
               'profiles', 'recent'},
    'trigger_reason': {'time_word', 'memory_question', 'object_hit'},
    'reason_code': {'not_selected_by_extractor', 'drop_low_information',
                    'source_backed_extraction', 'source_support_limited',
                    'same_correction_sources_grouped', 'extraction_failed',
                    'scope_required', 'scope_forbidden', 'not_recorded', 'unknown',
                    'found', 'suspected', 'absent', 'not_checked', 'allowed',
                    'archive', 'restore', 'soft', 'hard', 'purge', 'link_rejected'},
    'authz': {'ok', 'denied'},
    'error_type': {'ValueError', 'TypeError', 'RuntimeError', 'TimeoutError',
                   'JSONDecodeError', 'unclassified_error'},
}


# Fixed server-side targets, using console_api's path definitions, without importing it.
_BRIDGE_DIR = Path(os.environ.get('FISH_BRIDGE_DIR', r'C:\Users\34318\Apps\qq-bridge'))
# The 8781 managed launcher sends stdout to this file in its installation root.
LOG_TARGETS = {
    'memory': Path(__file__).resolve().parents[2] / 'memory-core-8781b.log',
    'bridge': _BRIDGE_DIR / 'bridge-run.log',
}


class PanelError(ValueError):
    def __init__(self, error, code=400):
        self.error, self.code = error, code
        super().__init__(error)


def _integer(value, name, low=1, high=2**63 - 1):
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,19}', value):
        value = int(value)
    if type(value) is not int or not low <= value <= high:
        raise PanelError('invalid_' + name)
    return value


def _number(value, high=253402300799):
    if type(value) not in (int, float) or not 0 <= value <= high or not math.isfinite(value):
        return None
    return value


def _unique_pairs(pairs):
    value = {}
    for key, part in pairs:
        if key in value:
            raise ValueError('duplicate_json_key')
        value[key] = part
    return value


def _no_constant(value):
    raise ValueError('non_finite_json')


def _json_dict(value):
    try:
        parsed = json.loads(value, object_pairs_hook=_unique_pairs,
                            parse_constant=_no_constant) if isinstance(value, (str, bytes)) else value
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {}


def _b64(blob):
    return base64.urlsafe_b64encode(blob).decode('ascii').rstrip('=')


def _page_options(data, scope, now):
    status = data.get('status', 'active')
    if not isinstance(status, str) or status not in ('active', 'archived', 'all'):
        raise PanelError('invalid_status')
    limit = _integer(data.get('limit', 50), 'limit', high=100)
    binding = hashlib.sha256(json.dumps(dict(scope=scope, status=status, limit=limit),
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    options = dict(status=status, limit=limit, binding=binding, as_of=now, ceiling=None, last=None)
    if 'cursor' not in data:
        return options
    try:
        cursor = data['cursor']
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1500:
            raise ValueError()
        payload, signature = cursor.split('.')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', payload) or not re.fullmatch(r'[0-9a-f]{64}', signature):
            raise ValueError()
        expected = hmac.new(_CURSOR_KEY, payload.encode('ascii'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        decoded = _json_dict(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        if set(decoded) != {'v', 'binding', 'as_of', 'ceiling', 'last'} or type(decoded['v']) is not int or decoded['v'] != 1:
            raise ValueError()
        stamp = _number(decoded['as_of'])
        if decoded['binding'] != binding or stamp is None or not now - CURSOR_SECONDS <= stamp <= now + 5:
            raise ValueError()
        ceiling = _integer(decoded['ceiling'], 'cursor')
        last = _integer(decoded['last'], 'cursor', high=ceiling)
        options.update(as_of=stamp, ceiling=ceiling, last=last)
        return options
    except (ValueError, TypeError, UnicodeError):
        raise PanelError('invalid_or_mismatched_cursor') from None


def _cursor(options, last):
    payload = _b64(json.dumps(dict(v=1, binding=options['binding'], as_of=options['as_of'],
        ceiling=options['ceiling'], last=last), separators=(',', ':')).encode())
    return payload + '.' + hmac.new(_CURSOR_KEY, payload.encode('ascii'), hashlib.sha256).hexdigest()


class _Inventory:
    """One read's ownership predicate and whitelist, shared by every projection."""
    def __init__(self, memory, scope):
        self.memory, self.scope, self.conn = memory, scope, memory.store.conn
        self.key = scope_key(scope)
        self.tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.link_columns = {r[1] for r in self.conn.execute('PRAGMA table_info(memory_links)')}
        self._memories, self._links = {}, {}

    def owned(self, row):
        return (row['status'] in ('active', 'archived')
                and owns_memory(self.memory, row, self.scope)
                and memory_scope(self.conn, row) == self.scope)

    def get(self, mid):
        if mid not in self._memories:
            row = self.conn.execute('SELECT ' + _MEMORY_COLUMNS + ' FROM memories WHERE id=?', (mid,)).fetchone()
            self._memories[mid] = row if row is not None and self.owned(row) else None
        return self._memories[mid]

    def evidence_ids(self, meta):
        if 'memory_pipeline_raw' not in self.tables:
            return []
        refs = meta.get('source_refs')
        candidates = [meta.get('raw_id')]
        if isinstance(refs, list):
            candidates += [r.get('raw_id') for r in refs[:32] if isinstance(r, dict)]
        result = []
        for rid in candidates:
            if not isinstance(rid, str) or not _SAFE_ID.fullmatch(rid) or rid in result:
                continue
            if self.registered_id('raw_id', rid):
                result.append(rid)
        return result[:32]

    def registered_id(self, key, value):
        if key == 'memory_id':
            return type(value) is int and 0 < value < 2**63 and self.get(value) is not None
        if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
            return False
        if key == 'raw_id':
            return 'memory_pipeline_raw' in self.tables and self.conn.execute(
                'SELECT 1 FROM memory_pipeline_raw WHERE raw_id=? AND scope=? AND session=?',
                (value, self.key, raw_session(self.scope))).fetchone() is not None
        if key == 'query_id':
            return 'judgment_ledger' in self.tables and self.conn.execute(
                "SELECT 1 FROM judgment_ledger WHERE scope=? AND source='auto:gate1' AND expected=? LIMIT 1",
                (self.key, value)).fetchone() is not None
        # Table names are fixed owning registries, never request values.
        return any(table in self.tables and self.conn.execute(
            'SELECT 1 FROM ' + table + ' WHERE scope=? AND request_id=? LIMIT 1',
            (self.key, value)).fetchone() is not None for table in (
                'memory_retirement_audit', 'memory_retirement_receipts', 'memory_version_events'))

    def link(self, lid):
        if lid in self._links:
            return self._links[lid]
        self._links[lid] = None
        if 'memory_links' not in self.tables or type(lid) is not int:
            return None
        fields = ('id', 'a_id', 'b_id', 'a_kind', 'b_kind', 'relation', 'created_at',
                  *_LINK_TIMES, 'superseded_by', 'stance_tag', 'weight')
        columns = ','.join(key for key in fields if key in self.link_columns)
        row = self.conn.execute('SELECT ' + columns + ' FROM memory_links WHERE id=?', (lid,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        if (value['a_kind'] != 'memory' or value['b_kind'] != 'memory'
                or self.get(value['a_id']) is None or self.get(value['b_id']) is None):
            return None
        sidecar = None
        if 'memory_pipeline_link_meta' in self.tables:
            sidecar = self.conn.execute('SELECT scope,state FROM memory_pipeline_link_meta WHERE link_id=?',
                                        (lid,)).fetchone()
        if sidecar is not None:
            if sidecar['scope'] != self.key:
                return None
            value['state'] = sidecar['state']
        else:
            # Older or manually created temporal edges have their own scoped registry.
            if 'memory_fact_versions' not in self.tables or self.conn.execute(
                    'SELECT 1 FROM memory_fact_versions WHERE scope=? AND link_id=? AND memory_id IN (?,?)',
                    (self.key, lid, value['a_id'], value['b_id'])).fetchone() is None:
                return None
            value['state'] = 'active'
        self._links[lid] = value
        return value

    def version_row(self, mid):
        if 'memory_fact_versions' not in self.tables:
            return None
        row = self.conn.execute('SELECT memory_id,link_id,fact_key FROM memory_fact_versions '
                                'WHERE scope=? AND memory_id=?', (self.key, mid)).fetchone()
        if row is None:
            return None
        link = self.link(row['link_id'])
        return dict(row) if link and link['state'] == 'active' and mid in (link['a_id'], link['b_id']) else None

    def version(self, row):
        version = self.version_row(row['id'])
        if version is None:
            known = {'memory_fact_versions', 'memory_links'} <= self.tables
            return dict(state='unversioned' if known else 'unknown')
        link = self.link(version['link_id'])
        successor = self.link(link.get('superseded_by'))
        if successor and successor['state'] != 'active':
            successor = None
        tag = link.get('stance_tag')
        return dict(state=tag if tag in ('active', 'transition', 'superseded') else 'unknown',
                    link_id=link['id'], superseded_by=successor['id'] if successor else None,
                    **{key: _number(link.get(key)) for key in _LINK_TIMES})

    def item(self, row):
        meta = _json_dict(row['meta'])
        stance, support = meta.get('stance'), meta.get('support_scope')
        if (not isinstance(stance, str) or stance not in STANCES
                or not isinstance(support, str) or support not in DIRECT | CLUE | {'不可用'}
                or stance == '他说' and meta.get('send_role') != 'user'
                or stance == '助手自述' and meta.get('send_role') != 'assistant'
                or stance not in ('他说', '助手自述') and support in DIRECT):
            stance, support = '来源不明', '仅可作线索'
        kind = row['kind']
        return dict(id=row['id'], kind=kind if isinstance(kind, str) and re.fullmatch(r'[a-z_]{1,32}', kind) else 'unknown',
                    text=row['text'] if isinstance(row['text'], str) else '', status=row['status'],
                    importance=_number(row['importance'], 1), created_at=_number(row['created_at']),
                    archived_at=_number(row['archived_at']), stance=stance, support_scope=support,
                    evidence_raw_ids=self.evidence_ids(meta), version=self.version(row))

    def links(self, mid):
        if 'memory_links' not in self.tables:
            return []
        registries = [f'SELECT link_id FROM {table} WHERE scope=?'
                      for table in ('memory_pipeline_link_meta', 'memory_fact_versions') if table in self.tables]
        if not registries:
            return []
        rows = self.conn.execute('SELECT id FROM memory_links WHERE a_kind=\'memory\' AND b_kind=\'memory\' '
            'AND (a_id=? OR b_id=?) AND id IN (' + ' UNION '.join(registries) + ') ORDER BY id',
            (mid, mid, *[self.key for _ in registries])).fetchmany(MAX_RELATED + 1)
        if len(rows) > MAX_RELATED:
            raise PanelError('too_many_related_records', 413)
        result = []
        for row in rows:
            link = self.link(row['id'])
            if link is None:
                continue
            result.append(dict(id=link['id'], a_id=link['a_id'], b_id=link['b_id'],
                relation=link['relation'] if link['relation'] in _RELATIONS else 'unknown',
                state=link['state'] if link['state'] in ('active', 'removed') else 'unknown',
                created_at=_number(link['created_at'])))
        return result

    def chain(self, mid):
        version = self.version_row(mid)
        if version is None:
            return []
        rows = self.conn.execute('SELECT memory_id FROM memory_fact_versions WHERE scope=? AND fact_key=? '
                                'ORDER BY link_id DESC', (self.key, version['fact_key'])).fetchmany(MAX_RELATED + 1)
        if len(rows) > MAX_RELATED:
            raise PanelError('too_many_related_records', 413)
        return [self.item(row) for part in rows
                if (row := self.get(part['memory_id'])) is not None and self.version_row(row['id']) is not None]


_INVENTORY_NOTICE = ('仅展示 owns_memory 与明确归属一致的 active/archived 库存；'
    '原话、引文、元数据及未知归属不下发。证据ID仅取本范围登记记录；版本状态未经原话复核。'
    '游标固定插入上界，状态按每次读取时的库存计算，不代表历史快照。')


def _memories(memory, data, scope, now):
    options = _page_options(data, scope, now)
    with memory.lock, memory.store._lock:
        inventory = _Inventory(memory, scope)
        rows = inventory.conn.execute('SELECT ' + _MEMORY_COLUMNS + " FROM memories WHERE status IN ('active','archived') "
            'AND created_at<=? AND (? IS NULL OR id<=?) ORDER BY id DESC',
            (options['as_of'], options['ceiling'], options['ceiling'])).fetchmany(MAX_INVENTORY + 1)
        if len(rows) > MAX_INVENTORY:
            raise PanelError('inventory_too_large', 413)
        owned = [row for row in rows if inventory.owned(row)]
        # The cursor never encodes a foreign memory's ID or a global count.
        if options['ceiling'] is None:
            options['ceiling'] = max((row['id'] for row in owned), default=0)
        counts = {state: sum(row['status'] == state for row in owned) for state in ('active', 'archived')}
        selected = [row for row in owned if options['status'] == 'all' or row['status'] == options['status']]
        remaining = [row for row in selected if options['last'] is None or row['id'] < options['last']]
        page = remaining[:options['limit']]
        more = len(remaining) > len(page)
        return dict(items=[inventory.item(row) for row in page], total=len(selected), counts=counts,
                    next_cursor=_cursor(options, page[-1]['id']) if more else None, has_more=more,
                    as_of=options['as_of'], scope=scope, coverage_notice=_INVENTORY_NOTICE)


def _memory(memory, data, scope, now):
    mid = _integer(data.get('id'), 'id')
    flag = data.get('include_versions', 0)
    if isinstance(flag, str) and flag not in ('0', '1'):
        raise PanelError('invalid_include_versions')
    versions = _integer(flag, 'include_versions', low=0, high=1)
    with memory.lock, memory.store._lock:
        inventory = _Inventory(memory, scope)
        row = inventory.get(mid)
        if row is None:
            raise PanelError('memory_not_found', 404)
        result = dict(item=inventory.item(row), links=inventory.links(mid), scope=scope,
                      as_of=now, coverage_notice=_INVENTORY_NOTICE)
        if versions:
            result['versions'] = inventory.chain(mid)
        return result


def _log_item(record, scope, now, inventory):
    if record.get('scope') != scope or any(key in record and record[key] != scope[key] for key in _SCOPE_FIELDS):
        return None
    event = record.get('event')
    if not isinstance(event, str) or event not in _LOG_ENUMS['event']:
        return None
    result = {}
    for key, allowed in _LOG_ENUMS.items():
        value = record.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    stamp = _number(record.get('at'), now + 5)
    if stamp is not None:
        result['at'] = stamp
    for key in ('query_id', 'request_id', 'raw_id'):
        value = record.get(key)
        if inventory.registered_id(key, value):
            result[key] = value
    value = record.get('memory_id')
    if inventory.registered_id('memory_id', value):
        result['memory_id'] = value
    if type(record.get('scope_applied')) is bool:
        result['scope_applied'] = record['scope_applied']
    return result


def _log_record(line):
    if line.startswith(b'[scope_recall] '):
        record = _json_dict(line.removeprefix(b'[scope_recall] '))
        if record:
            record['event'] = 'scope_recall'
        return record
    if line.startswith(b'[memory-gate1] '):
        return _json_dict(line.removeprefix(b'[memory-gate1] '))
    return _json_dict(line)


def _logs(memory, data, scope, now):
    target = data.get('target')
    if not isinstance(target, str) or target not in ('memory', 'bridge'):
        raise PanelError('invalid_log_target')
    lines = _integer(data.get('lines', 100), 'lines', high=500)
    result = dict(items=[], scope=scope, target=target, as_of=now,
                  scanned_bytes=0, scanned_lines=0, truncated=False, source_status='read',
                  coverage_notice='只扫描有限尾部窗口中能解析且三绑定一致的结构化诊断。'
                    '未登记的标识符不下发。未归属、非结构化或未记录阶段未知，'
                    '没有可安全展示的归属记录不等于没有事件。')
    deadline = time.monotonic() + MAX_LOG_SECONDS
    if time.monotonic() >= deadline:
        result['truncated'] = True
        return result
    try:
        path = LOG_TARGETS.get(target)
        if path is None:
            raise OSError('log_target_unavailable')
        with path.open('rb') as stream:
            end = stream.seek(0, os.SEEK_END)
            start = max(0, end - MAX_LOG_BYTES)
            stream.seek(start)
            blob = stream.read(min(end - start, MAX_LOG_BYTES))
    except OSError:
        result['source_status'] = 'unavailable'
        result['coverage_notice'] = '日志目标不可用；未读取全局替代文件，该阶段未知。'
        return result
    result['scanned_bytes'] = len(blob)
    result['truncated'] = start > 0
    window = blob.splitlines(keepends=True)
    if start and window:
        window = window[1:]
    if window and not window[-1].endswith(b'\n'):
        window.pop()
        result['truncated'] = True
    items = []
    with memory.lock, memory.store._lock:
        inventory = _Inventory(memory, scope)
        for line in reversed(window):
            if result['scanned_lines'] >= MAX_LOG_SCAN_LINES or time.monotonic() >= deadline:
                result['truncated'] = True
                break
            result['scanned_lines'] += 1
            if len(line) > MAX_LOG_LINE_BYTES:
                result['truncated'] = True
                continue
            record = _log_record(line)
            item = _log_item(record, scope, now, inventory)
            if item is not None:
                if len(items) == lines:
                    result['truncated'] = True
                    break
                items.append(item)
    result['items'] = list(reversed(items))
    return result


def handle(memory, method, path, data):
    """Match ledger_panel.handle's envelope; data contains flattened query values.

    Detail's include_versions accepts only integers 0/1 or exact strings "0"/"1".
    No route authenticates credentials, reads raw archives, or calls service methods.
    """
    try:
        if path not in ROUTES:
            raise PanelError('unknown_panel_route', 404)
        if method != 'GET':
            raise PanelError('panel_read_only', 405)
        if not isinstance(data, dict):
            raise PanelError('invalid_panel_request')
        allowed = ({'status', 'limit', 'cursor'} if path == ROUTES[0] else
                   {'id', 'include_versions'} if path == ROUTES[1] else {'target', 'lines'})
        if set(data) - _SCOPE_FIELDS - allowed:
            raise PanelError('invalid_panel_options')
        scope = current(data).scope
        now = time.time()
        if path == ROUTES[0]:
            result = _memories(memory, data, scope, now)
        elif path == ROUTES[1]:
            result = _memory(memory, data, scope, now)
        else:
            result = _logs(memory, data, scope, now)
        return 200, dict(status='ok', data=result)
    except (PanelError, ScopeError) as error:
        return error.code, dict(status='error', error=error.error)
    except (ValueError, TypeError, OverflowError, RecursionError):
        return 400, dict(status='error', error='invalid_panel_request')
    except Exception:
        return 503, dict(status='error', error='panel_read_unavailable')
