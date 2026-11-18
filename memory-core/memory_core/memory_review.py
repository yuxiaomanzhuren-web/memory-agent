"""Scoped review observations, independent of memory facts and recall cooldowns.

HTTP adapters can pass flat scope fields or ``{"scope": {...}, ...}`` to
``stale`` and ``read``. Root configuration is reloaded per request, never taken
from request data. Each configured scope has exactly one root::

    {"roots": [{"scope": {"bot_id": "1", "persona": "p",
                "conv_key": "private:2"}, "root": "D:/approved-root"}]}

Only successful controlled reads and ``record_memory_access(scope, ids)`` write
``<store filename stem>.review.sqlite``. Constructor, stale and status do not
create it. Writes use their own short SQLite connections/transactions. No
contents, queries, credentials, or memory text are persisted there. A file hash
describes bytes at the recorded observation, not a subsequent filesystem state.
``read`` pages by decoded UTF-8 character offset (default 0), with a 2000-character
default page. ``next_offset`` is null at EOF. Access logs do not assert that a
caller has read, understood or consumed a whole document, even across pages.
External file reads and uninstrumented memory reads cannot be observed.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import closing, nullcontext
from pathlib import Path, PureWindowsPath

from .memory_evidence import owns_memory, scope_from, scope_key


DAY = 86400
MAX_LIMIT = 500
MAX_CHARS = 20000
TEXT_EXTENSIONS = {'.md', '.txt'}
SKIP_DIRS = {'.git', '__pycache__'}
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS file_access(
 scope TEXT NOT NULL, root_id TEXT NOT NULL, path TEXT NOT NULL,
 at REAL NOT NULL, access_count INTEGER NOT NULL,
 sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, returned_chars INTEGER NOT NULL,
 read_offset INTEGER NOT NULL DEFAULT 0, total_chars INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(scope, root_id, path));
CREATE TABLE IF NOT EXISTS memory_access(
 scope TEXT NOT NULL, memory_id INTEGER NOT NULL, at REAL NOT NULL,
 access_count INTEGER NOT NULL, PRIMARY KEY(scope, memory_id));
'''


class ReviewError(ValueError):
    """ValueError-compatible adapter error with an optional HTTP status code."""

    def __init__(self, error, code=400):
        super().__init__(error)
        self.error, self.code = error, code


def _scope(data):
    if not isinstance(data, dict):
        raise ReviewError('invalid_scope')
    if any(k in data for k in ('root', 'roots', 'root_path', 'roots_path')):
        raise ReviewError('request_roots_forbidden')
    try:
        scope = scope_from(data.get('scope', data))
        if 'scope' in data and any(k in data and str(data[k]) != scope[k] for k in scope):
            raise ValueError('conflicting_scope')
        return scope
    except ValueError as exc:
        raise ReviewError(str(exc)) from exc


def _number(data, name, default, integer=False, cap=None, minimum=None):
    value = data.get(name, default)
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError
        if integer and not isinstance(value, (str, int)):
            raise ValueError
        number = int(value) if integer else float(value)
        if not math.isfinite(number) or number < (minimum if minimum is not None else 1 if integer else 0):
            raise ValueError
    except (ValueError, TypeError, OverflowError) as exc:
        raise ReviewError('invalid_' + name) from exc
    return min(number, cap) if cap is not None else number


def _timestamp(value):
    try:
        if isinstance(value, bool):
            return None
        stamp = float(value)
        return stamp if math.isfinite(stamp) and stamp >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _file_key(path):
    # Windows spelling/case aliases share an observation, but distinct roots do not.
    return os.path.normcase(str(path)).replace('\\', '/')


class MemoryReview:
    def __init__(self, memory, roots_path=None, clock=time.time):
        self.memory, self.store, self.clock = memory, memory.store, clock
        self.roots_path = Path(roots_path) if roots_path is not None else None
        self.sidecar_path = Path(self.store.path).with_suffix('.review.sqlite')

    def _now(self):
        now = _timestamp(self.clock())
        if now is None:
            raise ReviewError('invalid_clock')
        return now

    def _root(self, scope):
        config = self.roots_path or Path(os.environ.get('MEMORY_REVIEW_ROOTS_PATH') or
                                        Path(__file__).with_name('memory_review_roots.json'))
        try:
            value = json.loads(config.read_text(encoding='utf-8-sig'))
            if not isinstance(value, dict) or not isinstance(value.get('roots'), list):
                raise ValueError
            roots = {}
            for entry in value['roots']:
                key = scope_key(entry['scope'])
                root = entry['root']
                if key in roots or not isinstance(root, str) or not root or not Path(root).is_absolute():
                    raise ValueError
                roots[key] = root
            selected = roots.get(scope_key(scope))
            # Never even stat/resolve a different scope's configured root.
            return Path(selected).resolve() if selected is not None else None
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise ReviewError('invalid_review_roots', 503) from exc

    @staticmethod
    def _root_id(root):
        return hashlib.sha256(os.path.normcase(str(root)).encode('utf-8')).hexdigest()

    @staticmethod
    def _coverage(root):
        return {
            'status': 'partial',
            'files': {'root_configured': root is not None,
                      'root_available': root is not None and root.is_dir(),
                      'extensions': sorted(TEXT_EXTENSIONS),
                      'basis': 'controlled_review_read',
                      'full_read_claimed': False,
                      'uses_filesystem_atime': False, 'external_reads_observable': False},
            'memories': {'basis': ['review_sidecar', 'memory_surface_log',
                                   'last_access_when_access_count_positive'],
                         'ownership': 'exact_owns_memory', 'status_filter': 'active',
                         'uninstrumented_reads_observable': False},
            'unknown_means': 'No observed access; unknown is not evidence of never accessed or stale.',
            'notice': '仅观测访问或局部读取、verify命中留痕及已有访问记录；不代表通读，工具外读取不可知。'
        }

    def status(self, data):
        """Optional adapter hook: coverage only, no scans or observation writes."""
        scope = _scope(data)
        return {'scope': scope, 'coverage': self._coverage(self._root(scope)),
                'sidecar_exists': self.sidecar_path.is_file()}

    def _observations(self, key, root_id):
        if not self.sidecar_path.exists():
            return {}, {}
        # mode=ro never initializes a database; missing tables/corruption fail closed.
        try:
            with closing(sqlite3.connect(self.sidecar_path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                files = {r['path']: dict(r) for r in conn.execute(
                    'SELECT * FROM file_access WHERE scope=? AND root_id=?', (key, root_id))}
                memories = {r['memory_id']: r['at'] for r in conn.execute(
                    'SELECT memory_id,at FROM memory_access WHERE scope=?', (key,))}
            return files, memories
        except sqlite3.Error as exc:
            raise ReviewError('review_observations_unavailable', 503) from exc

    def _owned(self, scope):
        with getattr(self.store, '_lock', None) or nullcontext():
            rows = self.store.conn.execute("SELECT * FROM memories WHERE status='active'").fetchall()
            return [dict(row) for row in rows if owns_memory(self.memory, row, scope)]

    def _surface_times(self, key):
        with getattr(self.store, '_lock', None) or nullcontext():
            try:
                return dict(self.store.conn.execute(
                    'SELECT memory_id,MAX(at) FROM memory_surface_log WHERE scope=? GROUP BY memory_id', (key,)))
            except sqlite3.OperationalError as exc:
                if 'no such table' not in str(exc):
                    raise
                return {}

    @staticmethod
    def _files(root):
        if root is None or not root.is_dir():
            return
        def onerror(_):
            raise ReviewError('review_files_unavailable', 503)
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
            parent = Path(directory)
            dirs[:] = sorted(d for d in dirs if d.casefold() not in SKIP_DIRS
                             and not _linked(parent / d)
                             and (parent / d).resolve().is_relative_to(root))
            for name in sorted(names):
                path = parent / name
                if (path.suffix.lower() in TEXT_EXTENSIONS and not _linked(path)
                        and path.resolve().is_relative_to(root) and path.is_file()):
                    yield path

    @staticmethod
    def _access(item, observations, now, days):
        valid = {source: stamp for source, value in observations.items()
                 if (stamp := _timestamp(value)) is not None}
        last = max(valid.values()) if valid else None
        age = max(0.0, (now - last) / DAY) if last is not None else None
        item.update(last_access=last, age_days=age, access_sources=sorted(valid),
                    access_status='unknown' if last is None else
                    'stale' if now - last >= days * DAY else 'recent')
        return item

    def stale(self, data):
        scope = _scope(data)
        days = _number(data, 'days', 7)
        limit = _number(data, 'limit', 50, integer=True, cap=MAX_LIMIT)
        now, root = self._now(), self._root(scope)
        root_id = self._root_id(root) if root is not None else None
        key = scope_key(scope)
        file_access, memory_access = self._observations(key, root_id)
        buckets = {name: [] for name in ('stale_files', 'untracked_files', 'stale_memories', 'untracked_memories')}
        counts = dict(files=0, tracked_files=0, memories=0, tracked_memories=0)
        for path in self._files(root):
            relative = path.relative_to(root).as_posix()
            observed = file_access.get(_file_key(relative))
            stat = path.stat()
            item = self._access({'path': relative, 'root_id': root_id, 'size_bytes': stat.st_size,
                                 'mtime': stat.st_mtime, 'sha256': observed['sha256'] if observed else None},
                                {'review_read': observed['at']} if observed else {}, now, days)
            counts['files'] += 1
            counts['tracked_files'] += item['last_access'] is not None
            if item['access_status'] != 'recent':
                buckets['untracked_files' if item['last_access'] is None else 'stale_files'].append(item)
        surfaced = self._surface_times(key)
        for row in self._owned(scope):
            mid = row['id']
            observed = {}
            if mid in memory_access:
                observed['review_sidecar'] = memory_access[mid]
            if mid in surfaced:
                observed['memory_surface_log'] = surfaced[mid]
            if row.get('access_count', 0) > 0:
                observed['last_access'] = row.get('last_access')
            item = self._access({'id': mid, 'kind': row['kind'], 'text': str(row['text'])[:240]},
                                observed, now, days)
            counts['memories'] += 1
            counts['tracked_memories'] += item['last_access'] is not None
            if item['access_status'] != 'recent':
                buckets['untracked_memories' if item['last_access'] is None else 'stale_memories'].append(item)
        for name, items in buckets.items():
            items.sort(key=lambda item: (item['last_access'] if item['last_access'] is not None else -1,
                                         item.get('path', ''), item.get('id', 0)))
            counts[name] = len(items)
        counts['recent_files'] = counts['tracked_files'] - counts['stale_files']
        counts['recent_memories'] = counts['tracked_memories'] - counts['stale_memories']
        return {'scope': scope, 'observed_at': now, 'days': days, 'counts': counts,
                'coverage': self._coverage(root),
                'limits': {'per_category': limit, 'max_limit': MAX_LIMIT,
                           'truncated': {name: len(items) > limit for name, items in buckets.items()}},
                **{name: items[:limit] for name, items in buckets.items()}}

    @staticmethod
    def _read_path(root, relative):
        if root is None:
            raise ReviewError('review_root_not_allowed', 403)
        if (not isinstance(relative, str) or not relative or '\x00' in relative
                or ':' in relative or PureWindowsPath(relative).anchor or Path(relative).is_absolute()):
            raise ReviewError('invalid_review_path')
        parts = relative.replace('\\', '/').split('/')
        if any(p in ('', '.', '..') or p.endswith((' ', '.')) or p.casefold() in SKIP_DIRS for p in parts):
            raise ReviewError('invalid_review_path')
        path = root
        try:
            for part in parts:
                path = path / part
                if _linked(path):
                    raise ReviewError('review_path_link_forbidden', 403)
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ReviewError('review_path_outside_root', 403)
            if any(p.casefold() in SKIP_DIRS for p in resolved.relative_to(root).parts):
                raise ReviewError('invalid_review_path')
            if resolved.suffix.lower() not in TEXT_EXTENSIONS or not resolved.is_file():
                raise ReviewError('review_path_not_text')
            return resolved
        except (OSError, RuntimeError) as exc:
            raise ReviewError('review_path_unavailable', 404) from exc

    def _write(self, sql, rows):
        try:
            with closing(sqlite3.connect(str(self.sidecar_path), timeout=5)) as conn:
                conn.executescript(_SCHEMA)
                with conn:
                    # Upgrade only this sidecar when the first instrumented page
                    # follows an observation from the pre-pagination version.
                    columns = {row[1] for row in conn.execute('PRAGMA table_info(file_access)')}
                    for name in ('read_offset', 'total_chars'):
                        if name not in columns:
                            conn.execute(f'ALTER TABLE file_access ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
                    conn.executemany(sql, rows)
        except sqlite3.Error as exc:
            raise ReviewError('review_observation_write_failed', 503) from exc

    def read(self, data):
        scope = _scope(data)
        limit = _number(data, 'limit_chars', 2000, integer=True, cap=MAX_CHARS)
        offset = _number(data, 'offset', 0, integer=True, minimum=0)
        root = self._root(scope)
        path = self._read_path(root, data.get('path'))
        # Capture one byte stream for both the requested page and the full hash.
        digest, decoder = hashlib.sha256(), codecs.getincrementaldecoder('utf-8-sig')()
        content, total_chars, size = '', 0, 0
        try:
            checked = path.stat()
            with path.open('rb') as stream:
                opened = os.fstat(stream.fileno())
                if (not os.path.samestat(checked, opened) or
                        self._read_path(root, data['path']) != path):
                    raise ReviewError('review_file_changed', 409)
                while blob := stream.read(65536):
                    digest.update(blob)
                    size += len(blob)
                    text = decoder.decode(blob)
                    if '\x00' in text:
                        raise ReviewError('review_path_not_text')
                    start = max(0, offset - total_chars)
                    content += text[start:start + max(0, limit - len(content))]
                    total_chars += len(text)
                tail = decoder.decode(b'', final=True)
                start = max(0, offset - total_chars)
                content += tail[start:start + max(0, limit - len(content))]
                total_chars += len(tail)
                final = os.fstat(stream.fileno())
                if (opened.st_mtime_ns, opened.st_size) != (final.st_mtime_ns, final.st_size):
                    raise ReviewError('review_file_changed', 409)
        except UnicodeError as exc:
            raise ReviewError('review_file_not_utf8') from exc
        except OSError as exc:
            raise ReviewError('review_path_unavailable', 404) from exc
        at, root_id = self._now(), self._root_id(root)
        relative = path.relative_to(root).as_posix()
        sha = digest.hexdigest()
        truncated = offset + len(content) < total_chars
        self._write('''INSERT INTO file_access
            (scope,root_id,path,at,access_count,sha256,size_bytes,returned_chars,read_offset,total_chars)
            VALUES(?,?,?,?,1,?,?,?,?,?)
            ON CONFLICT(scope,root_id,path) DO UPDATE SET
            at=excluded.at, access_count=file_access.access_count+1,
            sha256=excluded.sha256,size_bytes=excluded.size_bytes,returned_chars=excluded.returned_chars,
            read_offset=excluded.read_offset,total_chars=excluded.total_chars''',
            [(scope_key(scope), root_id, _file_key(relative), at, sha, size, len(content), offset, total_chars)])
        return {'scope': scope, 'path': relative, 'root_id': root_id, 'content': content,
                'sha256': sha, 'size_bytes': size, 'observed_at': at, 'mtime': final.st_mtime,
                'offset': offset, 'next_offset': offset + len(content) if truncated else None,
                'total_chars': total_chars, 'returned_chars': len(content), 'limit_chars': limit,
                'truncated': truncated, 'observation_kind': 'access',
                'coverage': self._coverage(root)}

    def record_memory_access(self, scope, ids):
        """Observe actual active owned verify hits; never touch core last_access."""
        scope = _scope(scope)
        if (not isinstance(ids, (list, tuple, set)) or
                any(isinstance(mid, bool) or not isinstance(mid, int) or mid < 1 for mid in ids)):
            raise ReviewError('invalid_memory_ids')
        wanted = set(ids)
        owned = sorted(row['id'] for row in self._owned(scope) if row['id'] in wanted) if wanted else []
        at = self._now()
        if owned:
            self._write('''INSERT INTO memory_access VALUES(?,?,?,1)
                ON CONFLICT(scope,memory_id) DO UPDATE SET
                at=MAX(memory_access.at,excluded.at),access_count=memory_access.access_count+1''',
                [(scope_key(scope), mid, at) for mid in owned])
        return {'scope': scope, 'ids': owned, 'recorded': len(owned), 'observed_at': at,
                'tracking': 'review_sidecar', 'coverage': self._coverage(None)['memories']}