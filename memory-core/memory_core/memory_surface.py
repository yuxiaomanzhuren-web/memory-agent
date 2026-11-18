"""③ 该想起时会浮起：话题闸门 + 加权浮起 + 可解释的「为什么现在想起」。

与 turn_recall 的区别：turn_recall 回答「我昨天跟你说过什么」（原句/日期命中）；
这里回答「他现在聊到这件事，有什么旧事该自己冒出来」。三条硬规矩：
  1. 话题词对不上就不塞，重要度再高也不进提示词（守第二批验收第 3 条）。
  2. 最多 1–3 条；排序权重 最近未提过 > 重要度 > 话题词重合。
  3. 每一条都带 why_now，写清「为什么现在想起它」，答不出就不算命中。
浮起留痕放在独立连接上，绝不改写原文库、也不碰别的线程的事务。
"""
from __future__ import annotations
from .source_provenance import aggregate, readable, rank, constrain

import sqlite3
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from .memory_evidence import _metadata, _timestamp, _verify_record, owns_memory, raw_session, scope_from, scope_key
from .turn_recall import MAX_CONTEXT_CHARS, _assigned_records, _overlap, _query

SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')
W_UNMENTIONED, W_IMPORTANCE, W_OVERLAP = 0.5, 0.3, 0.2
COOL_DAYS = 7.0          # 满 7 天没提起，未提过权重给满
REPEAT_GUARD_HOURS = 2.0  # 两小时内刚浮起过的同一条不再浮起，避免永远是那几条
MAX_HITS = 3
MAX_QUOTE_CHARS = 160

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_surface_log(
 id INTEGER PRIMARY KEY,scope TEXT NOT NULL,memory_id INTEGER NOT NULL,
 at REAL NOT NULL,why_now TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_surface_log ON memory_surface_log(scope,memory_id,at);
"""

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def surface_recall(memory, clock=time.time):
    key = str(getattr(memory.store, 'path', id(memory.store)))
    with _CACHE_LOCK:
        obj = _CACHE.get(key)
        if obj is None:
            obj = SurfaceRecall(memory, clock=clock)
            _CACHE[key] = obj
        return obj


def _empty(why, error=None):
    out = {'hits': [], 'block': '', 'why': why}
    if error:
        out['error'] = error
    return out


def _refs_for(meta, scope, records):
    """只保留能核验的来源：raw 逐字回查，或账本自带的 daybook 引用。"""
    refs = meta.get('source_refs')
    if refs is None and all(k in meta for k in ('raw_id', 'raw_ts', 'quote')):
        refs = [{k: meta[k] for k in ('raw_id', 'raw_ts', 'quote')}]
    if not isinstance(refs, list):
        return []
    out = []
    for ref in refs[:2]:
        if not isinstance(ref, dict) or not isinstance(ref.get('quote'), str) or not ref['quote'].strip():
            continue
        if ref.get('source_kind') == 'daybook':
            if (isinstance(ref.get('page_id'), int) and isinstance(ref.get('revision'), int)
                    and ref.get('field') in ('direction', 'finding') and isinstance(ref.get('day'), str)):
                out.append(dict(ref))
        else:
            try:
                checked = _verify_record(scope, ref, records)
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
            checked['source_kind'] = 'raw'
            if rank(checked)>0:out.append(checked)
    return out


def _scene(meta, refs):
    """① 最多两条现场线索：一句原话 + 一个时间/地点。"""
    quote = str(refs[0].get('quote', '') if refs else meta.get('quote', '')).strip()[:MAX_QUOTE_CHARS]
    when = str(meta.get('scene', '') or '').strip()
    if not when and refs and refs[0].get('raw_ts'):
        try:
            when = datetime.fromtimestamp(_timestamp(refs[0]['raw_ts']), SHANGHAI).strftime('%m-%d %H:%M')
        except (ValueError, TypeError, OverflowError, OSError):
            when = ''
    if not when and meta.get('day'):
        when = str(meta['day'])
    return quote, when


class SurfaceRecall:
    def __init__(self, memory, clock=time.time):
        self.memory, self.store, self.clock = memory, memory.store, clock
        self.main_conn = getattr(self.store.conn, 'raw', self.store.conn)
        self.log_conn = sqlite3.connect(str(self.store.path), timeout=5.0, check_same_thread=False)
        self.log_conn.row_factory = sqlite3.Row
        self.log_conn.execute('PRAGMA busy_timeout=5000')
        self.log_conn.executescript(SCHEMA)
        self.log_conn.commit()
        self._log_lock = threading.RLock()

    def _last_at(self, key, ids):
        if not ids:
            return {}
        marks = ','.join('?' for _ in ids)
        with self._log_lock:
            rows = self.log_conn.execute(
                f'SELECT memory_id,MAX(at) AS last FROM memory_surface_log WHERE scope=? AND memory_id IN ({marks}) GROUP BY memory_id',
                [key, *ids]).fetchall()
        return {int(r['memory_id']): float(r['last']) for r in rows}

    def _log(self, key, hits):
        try:
            with self._log_lock:
                self.log_conn.executemany(
                    'INSERT INTO memory_surface_log(scope,memory_id,at,why_now) VALUES(?,?,?,?)',
                    [(key, int(hit['id']), self.clock(), hit['why_now']) for hit in hits])
                self.log_conn.commit()
        except sqlite3.Error:
            # 留痕失败不让回忆消失，但下次可能重复浮起；宁可重复也不静默注入不可解释的内容。
            pass

    def recall(self, data) -> dict:
        try:
            scope = scope_from(data)
        except ValueError:
            return _empty('范围无效', 'invalid_scope')
        text = data.get('text', data.get('query', ''))
        if not isinstance(text, str) or not text.strip():
            return _empty('这一轮没有话题')
        try:
            _, tokens, _ = _query(text)
            if not tokens:
                return _empty('这一轮没有可匹配的话题词')
            key = scope_key(scope)
            lock = getattr(self.store, '_lock', None)
            with lock if lock is not None else nullcontext():
                rows = [dict(r) for r in self.main_conn.execute(
                    "SELECT * FROM memories WHERE status='active' ORDER BY id")]
                owned = [row for row in rows if owns_memory(self.memory, row, scope)]
                from .memory_temporal import filter_memories, wants_history
                owned = filter_memories(self.main_conn, owned, include_history=wants_history(data), now=self.clock())
                try:
                    index = [dict(r) for r in self.main_conn.execute(
                        'SELECT * FROM memory_pipeline_raw WHERE scope=? AND session=? ORDER BY raw_id',
                        (key, raw_session(scope)))]
                except sqlite3.OperationalError as exc:
                    if 'no such table' not in str(exc):
                        raise
                    index = []
            records = _assigned_records(self.memory.raw.root, scope, index)
            last_at = self._last_at(key, [row['id'] for row in owned])
            now = self.clock()
            candidates = []
            for row in owned:
                meta = _metadata(row)
                refs = [constrain(ref,meta,stage='surface_entry') for ref in _refs_for(meta, scope, records)]
                if not refs:
                    continue
                quote, when = _scene(meta, refs)
                topic = str(meta.get('topic') or meta.get('object') or '').strip()
                hay = ' '.join(str(x or '') for x in (row['text'], topic, meta.get('object'),
                                                      meta.get('event_key'), when, quote))
                matched = _overlap(tokens, hay)
                if not matched:  # 闸门：不相干的旧事，重要度再高也不进提示词
                    continue
                last = last_at.get(int(row['id']), 0.0)
                if last and now - last < REPEAT_GUARD_HOURS * 3600:
                    continue
                created = float(row['created_at'] or 0)
                since = max(created, last) if last else created
                age_days = max(0.0, (now - since) / 86400)
                unmentioned = min(1.0, age_days / COOL_DAYS)
                try:
                    importance = min(1.0, max(0.0, float(row['importance'])))
                except (TypeError, ValueError):
                    importance = 0.0
                overlap = min(1.0, len(matched) / max(1, len(tokens)))
                score = W_UNMENTIONED * unmentioned + W_IMPORTANCE * importance + W_OVERLAP * overlap
                score *= row.get('fact_version', {}).get('weight', 1.0)
                why_now = (f'为什么现在想起：你这句里的「{"、".join(matched[:4])}」'
                           f'对上了它当时的「{topic or row["text"][:16]}」共 {len(matched)} 处；'
                           f'已 {age_days:.1f} 天没提起（最近未提过优先）；重要度 {importance:.2f}。')
                candidates.append({
                    'id': row['id'], 'text': str(row['text']), 'kind': row['kind'],
                    'importance': importance, 'score': score, 'why': why_now, 'why_now': why_now,
                    'source_refs': refs, 'source_kind': refs[0].get('source_kind', 'raw'),
                    'source_role': refs[0].get('role', meta.get('source_role', 'user')), **aggregate(refs,stage='surface'),
                    'scene': when, 'quote': quote, 'matched': matched,
                })
            candidates.sort(key=lambda hit: (-hit['score'], -hit['importance'], str(hit['id'])))
            hits = candidates[:MAX_HITS]
            if not hits:
                return _empty('这一轮没有话题对得上的旧事，不硬塞')
            block = '\n'.join(
                f"- [{readable(hit)}] {hit['text']}（现场：{hit['scene'] or '时间未记'}｜原话：{hit['quote']}）\n  {hit['why_now']}"
                for hit in hits)[:MAX_CONTEXT_CHARS]
            self._log(key, hits)
            return {'hits': hits, 'block': block, 'why': '按话题闸门捞起，权重：最近未提过 > 重要度 > 话题词重合'}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, sqlite3.Error):
            return _empty('浮起暂不可用', 'surface_unavailable')
