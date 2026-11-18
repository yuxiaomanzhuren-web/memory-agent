"""Scoped canonical daybook and independently verified assistant projections."""
from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from .memory_evidence import scope_from, scope_key
from .operation_ledger import begin_change, finish_change
from .store import _token_blob
from .source_provenance import classify_source, normalize, aggregate
from .turn_recall import _tokens, _overlap, _speaker

SHANGHAI = timezone(timedelta(hours=8))
SCHEMA = '''
CREATE TABLE IF NOT EXISTS daybook_pages(
 id INTEGER PRIMARY KEY, scope TEXT NOT NULL, day TEXT NOT NULL,
 revision INTEGER NOT NULL, direction TEXT NOT NULL, findings TEXT NOT NULL,
 small TEXT NOT NULL, author TEXT NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(scope,day));
CREATE TABLE IF NOT EXISTS daybook_versions(
 page_id INTEGER NOT NULL, revision INTEGER NOT NULL, scope TEXT NOT NULL,
 day TEXT NOT NULL, direction TEXT NOT NULL, findings TEXT NOT NULL,
 small TEXT NOT NULL, author TEXT NOT NULL, updated_at REAL NOT NULL,
 reason TEXT NOT NULL, previous_revision INTEGER NOT NULL,
 PRIMARY KEY(page_id,revision));
CREATE TABLE IF NOT EXISTS daybook_projections(
 memory_id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL, revision INTEGER NOT NULL,
 field TEXT NOT NULL, field_index INTEGER NOT NULL,
 UNIQUE(page_id,revision,field,field_index));
CREATE TABLE IF NOT EXISTS memory_pipeline_owners(memory_id INTEGER PRIMARY KEY,scope TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_pipeline_link_meta(
 link_id INTEGER PRIMARY KEY,scope TEXT NOT NULL,source_refs TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'active',operator TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '',updated_at REAL NOT NULL);
'''


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class DaybookConflict(ValueError):
    code = 409


class Daybook:
    def __init__(self, memory, clock=time.time):
        self.memory, self.store, self.clock = memory, memory.store, clock
        self.conn = getattr(self.store.conn, 'raw', self.store.conn)
        with memory.lock, self.store._lock:
            if self.conn.in_transaction:
                raise RuntimeError('daybook requires its own transaction')
            self.conn.executescript(SCHEMA)

    @contextmanager
    def _tx(self):
        with self.memory.lock, self.store._lock:
            if self.conn.in_transaction:
                raise RuntimeError('daybook requires its own transaction')
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def _today(self):
        return datetime.fromtimestamp(self.clock(), SHANGHAI).date()

    def _day(self, data):
        value = data.get('day', self._today().isoformat())
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('invalid_day')
        date.fromisoformat(value)
        return value

    @staticmethod
    def _text(data, key, maximum, required=False):
        value = data.get(key, '')
        if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
            raise ValueError('invalid_' + key)
        return value

    @staticmethod
    def _page(row):
        if row is None:
            return None
        out = dict(row)
        out['findings'] = json.loads(out['findings'])
        out['scope'] = json.loads(out['scope'])
        return out

    def _select(self, data, scope):
        ident = data.get('page_id', data.get('id'))
        if ident is not None:
            if type(ident) is not int or ident <= 0:
                raise ValueError('invalid_page_id')
            sql, args = 'id=?', (ident,)
            if 'day' in data:
                sql += ' AND day=?'
                args += (self._day(data),)
        else:
            sql, args = 'day=?', (self._day(data),)
        return self.conn.execute('SELECT * FROM daybook_pages WHERE scope=? AND ' + sql,
                                 (scope_key(scope), *args)).fetchone()

    def get(self, data):
        scope = scope_from(data)
        with self.store._lock:
            return {'scope': scope, 'page': self._page(self._select(data, scope))}

    def versions(self, data):
        scope = scope_from(data)
        with self.store._lock:
            page = self._select(data, scope)
            rows = [] if page is None else self.conn.execute(
                'SELECT * FROM daybook_versions WHERE page_id=? AND scope=? ORDER BY revision',
                (page['id'], scope_key(scope))).fetchall()
            return {'scope': scope, 'versions': [self._page(row) for row in rows]}

    def write(self, data):
        """Replace the entire scoped daily page, guarded by expected_revision.

        direction, findings and small must all be supplied, including empty
        values to clear a field. Omission is a validation error, never a clear.
        author is required; changing direction/findings also requires reason.
        """
        scope = scope_from(data)
        for field in ('direction', 'findings', 'small'):
            if field not in data:
                raise ValueError('missing_' + field)
        key, day = scope_key(scope), self._day(data)
        expected = data.get('expected_revision')
        if type(expected) is not int or expected < 0:
            raise ValueError('invalid_expected_revision')
        direction = self._text(data, 'direction', 200)
        small = self._text(data, 'small', 200)
        author = self._text(data, 'author', 120, True)
        reason = self._text(data, 'reason', 1000)
        findings = data.get('findings', [])
        if not isinstance(findings, list) or len(findings) > 3 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 200 for item in findings):
            raise ValueError('invalid_findings')
        fields = (direction, _dump(findings), small, author)
        with self._tx():
            old = self.conn.execute('SELECT * FROM daybook_pages WHERE scope=? AND day=?', (key, day)).fetchone()
            revision = old['revision'] if old else 0
            same = old is not None and tuple(old[k] for k in ('direction', 'findings', 'small', 'author')) == fields
            if same:
                version = self.conn.execute('SELECT * FROM daybook_versions WHERE page_id=? AND revision=?',
                                            (old['id'], revision)).fetchone()
                if expected == revision or (expected == revision - 1 and version['reason'] == reason):
                    return self._page(old)
            if expected != revision:
                raise DaybookConflict('revision_conflict')
            if old and (old['direction'] != direction or json.loads(old['findings']) != findings) and not reason.strip():
                raise ValueError('change_requires_reason')
            now, revision = self.clock(), revision + 1
            if old:
                page_id = old['id']
                self.conn.execute('UPDATE daybook_pages SET revision=?,direction=?,findings=?,small=?,author=?,updated_at=? WHERE id=?',
                                  (revision, *fields, now, page_id))
                removal_reason = f'日记{day}第{revision - 1}版投影已被第{revision}版替代；' + (reason or '完整替换日记')
                retired = self.conn.execute("SELECT id FROM memories WHERE id IN (SELECT memory_id FROM daybook_projections WHERE page_id=?) AND status='active'", (page_id,)).fetchall()
                tickets = [begin_change(self.conn, row['id'], 'archive', removal_reason, author, actor='auto',
                                        scope=scope, now=now, reason_code='daybook_revision_replaced') for row in retired]
                self.conn.execute("UPDATE memories SET status='archived',archived_at=? WHERE id IN (SELECT memory_id FROM daybook_projections WHERE page_id=?) AND status='active'", (now, page_id))
                for ticket in tickets:
                    # Superseded projections cannot be independently restored by retirement.
                    finish_change(self.conn, ticket, reversible=False,
                                  details={'page_id': page_id, 'previous_revision': revision - 1, 'revision': revision})
                self.conn.execute('''UPDATE memory_pipeline_link_meta
                    SET state='removed',operator=?,reason=?,updated_at=?
                    WHERE scope=? AND state='active' AND link_id IN (
                        SELECT l.id FROM memory_links l JOIN daybook_projections p
                        ON (l.a_kind='memory' AND l.a_id=p.memory_id)
                        OR (l.b_kind='memory' AND l.b_id=p.memory_id)
                        WHERE p.page_id=? AND p.revision<?)''',
                    (author, removal_reason, now, key, page_id, revision))
            else:
                page_id = self.conn.execute('INSERT INTO daybook_pages(scope,day,revision,direction,findings,small,author,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                                            (key, day, revision, *fields, now)).lastrowid
            self.conn.execute('INSERT INTO daybook_versions(page_id,revision,scope,day,direction,findings,small,author,updated_at,reason,previous_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                              (page_id, revision, key, day, *fields, now, reason, revision - 1))
            projections = []
            if direction.strip():
                projections.append(('direction', -1, direction))
            projections.extend(('finding', i, text) for i, text in enumerate(findings))
            direction_id = None
            for field, index, quote in projections:
                ref = dict(source_kind='daybook', page_id=page_id, revision=revision, field=field, quote=quote, day=day)
                ref.update(classify_source({'r':'assistant','t':quote}))
                if field == 'finding':
                    ref['index'] = index
                meta = dict(source_kind='daybook', page_id=page_id, revision=revision, field=field,
                            index=index, day=day, source_role='assistant', pipeline_scope=scope, source_refs=[ref])
                meta.update(aggregate([ref],stage='daybook_birth'))
                mid = self.conn.execute("INSERT INTO memories(kind,text,importance,created_at,status,source,meta,visibility) VALUES('reflection',?,?,?,'active','daybook',?,'private')",
                                        (quote, .85 if field == 'direction' else .6, now, _dump(meta))).lastrowid
                self.conn.execute('INSERT INTO memories_fts(tokens,memory_id) VALUES(?,?)', (_token_blob(quote), mid))
                self.conn.execute('INSERT INTO memory_pipeline_owners VALUES(?,?)', (mid, key))
                self.conn.execute('INSERT INTO daybook_projections VALUES(?,?,?,?,?)', (mid, page_id, revision, field, index))
                if field == 'direction':
                    direction_id = mid
                elif direction_id is not None:
                    why = f'日记{day}第{revision}版查出[{index}]对应方向：{direction}'
                    a_id, b_id = sorted((mid, direction_id))
                    link = self.conn.execute("INSERT INTO memory_links(a_id,b_id,relation,why,review_id,created_at) VALUES(?,?,'因为',?,0,?)",
                                             (a_id, b_id, why, now)).lastrowid
                    self.conn.execute('INSERT INTO memory_pipeline_link_meta(link_id,scope,source_refs,operator,reason,updated_at) VALUES(?,?,?,?,?,?)',
                                      (link, key, _dump([ref]), author, why, now))
            return self._page(self.conn.execute('SELECT * FROM daybook_pages WHERE id=?', (page_id,)).fetchone())

    def recent(self, data):
        scope = scope_from(data)
        today = self._today()
        with self.store._lock:
            pages = [self._page(row) for row in self.conn.execute(
                'SELECT * FROM daybook_pages WHERE scope=? AND day IN (?,?) ORDER BY day DESC',
                (scope_key(scope), today.isoformat(), (today - timedelta(days=1)).isoformat()))]
        rendered = '\n'.join(f"{p['day']} [助手自写日记] 方向：{p['direction']}\n小事：{p['small']}" for p in pages)
        return {'scope': scope, 'pages': pages, 'rendered': rendered[:1200]}

    def recall(self, data):
        scope = scope_from(data)
        text = data.get('text', data.get('query', ''))
        empty = {'hits': [], 'block': '', 'why': '没有相关且已核验的日记'}
        if not isinstance(text, str) or not text.strip():
            return empty
        if _speaker(text) == 'user':
            return empty
        days = set()
        for token in re.findall(r'\d{4}-\d{2}-\d{2}|今天|昨天|\btoday\b|\byesterday\b', text, re.I):
            token = token.lower()
            if token in ('今天', 'today'):
                days.add(self._today().isoformat())
            elif token in ('昨天', 'yesterday'):
                days.add((self._today() - timedelta(days=1)).isoformat())
            else:
                try:
                    days.add(date.fromisoformat(token).isoformat())
                except ValueError:
                    return empty
        topic = re.sub(r'\d{4}-\d{2}-\d{2}|今天|昨天|\btoday\b|\byesterday\b', ' ', text, flags=re.I)
        request = re.sub(r'[\s，。？！?！：:、]', '', topic)
        explicit = bool(days and re.fullmatch(
            r'(?:请问|还记得|我想知道)?(?:(?:你|她)的)?(?:(?:账本|日记)(?:里|上)?的?)?'
            r'方向(?:那一行)?(?:原话)?(?:是什么|是啥|写了什么|怎么写的)?', request))
        tokens = _tokens(topic)
        hits = []
        with self.store._lock:
            rows = self.conn.execute('''SELECT m.*,p.page_id,p.revision AS projection_revision,p.field,p.field_index
                FROM daybook_projections p JOIN memories m ON m.id=p.memory_id
                JOIN memory_pipeline_owners o ON o.memory_id=m.id
                JOIN daybook_pages d ON d.id=p.page_id AND d.revision=p.revision
                WHERE d.scope=? AND o.scope=? AND m.status='active' ORDER BY d.day DESC,m.id''',
                (scope_key(scope), scope_key(scope))).fetchall()
            for row in rows:
                try:
                    version = self.conn.execute('SELECT * FROM daybook_versions WHERE page_id=? AND revision=? AND scope=?',
                                                (row['page_id'], row['projection_revision'], scope_key(scope))).fetchone()
                    if version is None or (days and version['day'] not in days):
                        continue
                    canonical = self.conn.execute('SELECT * FROM daybook_pages WHERE id=? AND scope=?',
                                                  (row['page_id'], scope_key(scope))).fetchone()
                    if canonical is None or any(canonical[k] != version[k] for k in
                            ('revision', 'day', 'direction', 'findings', 'small', 'author', 'updated_at')):
                        continue
                    field, index = row['field'], row['field_index']
                    if field == 'direction' and index == -1:
                        quote = version['direction']
                    elif field == 'finding' and type(index) is int and 0 <= index <= 2:
                        quote = json.loads(version['findings'])[index]
                    else:
                        continue
                    ref = dict(source_kind='daybook', page_id=row['page_id'], revision=row['projection_revision'], field=field, quote=quote, day=version['day'])
                    if field == 'finding':
                        ref['index'] = index
                    meta = json.loads(row['meta'])
                    stored_refs=meta.get('source_refs') or []
                    stored=stored_refs[0] if len(stored_refs)==1 else {}
                    ref.update(normalize(stored,stage='daybook_recall'))
                    expected_meta = dict(source_kind='daybook', page_id=row['page_id'], revision=row['projection_revision'], field=field,
                                         index=index, day=version['day'], source_role='assistant', pipeline_scope=scope, source_refs=[ref])
                    structural={k:v for k,v in ref.items() if k not in ('send_role','stance','support_scope','stance_reason','classifier_version','support_target')}
                    if row['text'] != quote or any(meta.get(k) != v for k, v in expected_meta.items() if k!='source_refs') or any(stored.get(k)!=v for k,v in structural.items()):
                        continue
                    matched = _overlap(tokens, quote) if tokens else []
                    if explicit:
                        if field != 'direction':
                            continue
                    elif not matched:
                        continue
                    why = f"助手自写日记；{version['day']}；第{row['projection_revision']}版；{field}"
                    hits.append(dict(id=row['id'], text=quote, kind=row['kind'], importance=row['importance'], why=why,
                                     source_kind='daybook', source_role='assistant', source_refs=[ref],
                                     **aggregate([ref],stage='daybook_recall'),
                                     score=1.0 if explicit else len(matched) / max(1, len(tokens))))
                except (ValueError, TypeError, KeyError, IndexError):
                    continue
        hits.sort(key=lambda hit: -hit['score'])
        hits = hits[:3]
        return dict(hits=hits, block='\n'.join('- [助手自写日记] ' + hit['text'] + '（' + hit['why'] + '）' for hit in hits),
                    why='已核验日记当前版本和投影' if hits else empty['why'])
