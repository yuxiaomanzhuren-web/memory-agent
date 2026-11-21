"""Read-only audit for absolute/modal wording in stored memories."""
from __future__ import annotations
import json
import re
from pathlib import Path

DEFAULT_TERMS = ('一直', '从不', '向来', '总是', '永远', '保证', '承认', '每次', '所有', '根本', '全部')

def load_terms(path=None):
    if path is None:
        return list(DEFAULT_TERMS)
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    terms = value.get('terms') if isinstance(value, dict) else value
    if not isinstance(terms, list) or any(not isinstance(t, str) or not t.strip() for t in terms):
        raise ValueError('terms must be a non-empty string list')
    return list(dict.fromkeys(t.strip() for t in terms))

def audit_rows(rows, terms=None):
    terms = load_terms(terms) if isinstance(terms, (str, Path)) else list(terms or DEFAULT_TERMS)
    pattern = re.compile('|'.join(re.escape(t) for t in terms))
    result = []
    for row in rows:
        text = str(row.get('text') or '')
        hits = list(dict.fromkeys(pattern.findall(text)))
        if not hits:
            continue
        meta = row.get('meta') or {}
        if isinstance(meta, str):
            try: meta = json.loads(meta)
            except Exception: meta = {}
        refs = meta.get('source_refs') if isinstance(meta, dict) else None
        if not isinstance(refs, list) or not refs:
            state = 'ungrounded'; excerpts = []
        else:
            excerpts = [str(r.get('quote') or '')[:300] for r in refs if isinstance(r, dict)]
            state = 'grounded' if all(any(term in quote for quote in excerpts) for term in hits) else 'overreach'
        result.append({'id': int(row['id']), 'state': state, 'terms': hits, 'evidence': excerpts})
    return result

def audit_db(conn, terms=None):
    rows = conn.execute('SELECT id,text,meta FROM memories WHERE status="active" ORDER BY id').fetchall()
    return audit_rows([dict(row) for row in rows], terms)
