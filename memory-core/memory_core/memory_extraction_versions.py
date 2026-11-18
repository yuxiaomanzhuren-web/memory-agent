"""Source-first splitting of paired, explicit counterclaims; no historical rewrite."""
import json
import re

from .memory_evidence import scope_key, _metadata, raw_records

MARKER = re.compile(r'说错了|说反了|纠正一下|更正一下|其实|不过|反倒|不是.{1,40}(?:而)?是')
EXPLICIT = re.compile(r'说错了|说反了|纠正一下|更正一下|其实不是|其实是|不是.{1,40}(?:而)?是')
ARC_TEXT = re.compile(r'改口|纠正|翻篇|他说错了')
# ponytail: a conservative literal opposition vocabulary, not general entailment.
# Unsupported meanings keep ordinary extraction and produce a review warning.
OPPOSITES = (
    ('口味', ('不好吃', '难吃', '不爱吃', '不喜欢吃'), ('好吃', '爱吃', '喜欢吃')),
    ('喜好', ('不喜欢', '讨厌'), ('喜欢',)),
    ('开关', ('不启用', '停用', '关闭'), ('启用', '开启')),
)


def claim(text):
    if re.search(r'不(?:觉得|认为|是说)|没(?:说|觉得)|不能说|并非|不代表|不意味着|并不是|谈不上',text):
        return None
    for dimension, negatives, positives in OPPOSITES:
        if any(word in text for word in negatives):
            return dimension, -1
        if any(word in text for word in positives):
            return dimension, 1
    return None


def counterclaim(older, newer):
    if re.search(r'好像|可能|也许|似乎|如果|假如|不确定|不认为|不觉得|并非|并不是|不能说|不代表',older+newer):
        return False
    a, b = claim(older), claim(newer)
    return bool((a and b and a[0] == b[0] and a[1] != b[1] and MARKER.search(newer))
                or (a is None and b is None and EXPLICIT.search(newer) and older != newer))


def warnings_for(text):
    return [dict(code='arc_in_entry_text', word=m.group(), start=m.start(), end=m.end())
            for m in ARC_TEXT.finditer(text)]


def _body(refs):
    parts = []
    for ref in refs:
        text = ref['quote']
        text = re.sub(r'^(?:(?:我)?(?:刚刚|刚才)?\s*(?:说错了|说反了)|纠正一下|更正一下)[，,。：:\s]*', '', text)
        text = re.sub(r'其实|不过|反倒', '', text).strip()
        parts.append(text)
    return '用户表示：' + '；并说明：'.join(parts)


def _previous_version(pipeline, scope, key, anchor, before):
    mapped = pipeline.conn.execute('''SELECT m.*,v.link_id FROM memory_fact_versions v
        JOIN memory_links l ON l.id=v.link_id JOIN memories m ON m.id=v.memory_id
        WHERE v.scope=? AND v.fact_key=? AND l.superseded_by IS NULL AND l.t_expired IS NULL
        ORDER BY l.id DESC LIMIT 1''', (scope_key(scope), key)).fetchone()
    if mapped:
        return dict(mapped)
    # A may have been extracted in an earlier job. Register its unchanged ID
    # only when a new paired claim arrives, never batch-rewrite historical links.
    from .memory_temporal import filter_memories
    rows = [dict(r) for r in pipeline.conn.execute('''SELECT m.* FROM memories m
        JOIN memory_pipeline_owners o ON o.memory_id=m.id WHERE o.scope=?
        AND m.status='active' AND m.created_at<? ORDER BY m.created_at DESC,m.id DESC LIMIT 100''',
                                                  (scope_key(scope), before))]
    for row in filter_memories(pipeline.conn, rows, now=pipeline.clock()):
        meta = _metadata(row)
        refs = meta.get('source_refs', [])
        if meta.get('object') != anchor or meta.get('source_kind') != 'raw_extract' or not refs:
            continue
        try:
            refs = pipeline._refs(scope, refs, max_refs=2)
            if any(r['role'] != 'user' or r['raw_ts'] >= before or before-r['raw_ts']>86400 for r in refs):
                continue
            signed = {claim(r['quote']) for r in refs}
            if len(signed) != 1 or (None not in signed and next(iter(signed))[0] != key.rsplit('|',1)[-1]):
                continue
            if warnings_for(row['text']):
                continue  # A legacy merged narrative is not a clean endpoint.
            return dict(row, link_id=None)
        except (ValueError, TypeError):
            continue
    return None


def prepare(pipeline, job, output):
    """Replace a merged model item only when complete originals prove a split.

    The model supplies grounded object anchors, never the transition verdict.
    No extra model request; ambiguous objects/oppositions are audited, not guessed.
    """
    if isinstance(output, str):
        text = output.strip()
        if text.startswith('```'):
            text = text.partition('\n')[2].rsplit('```', 1)[0]
        output = json.loads(text)
    if not isinstance(output, dict) or not isinstance(output.get('items'), list):
        return output, {}, []
    source_items = output['items']
    reports = [dict(code='arc_in_model_text', item_no=i, text=item.get('text', ''), warnings=warnings_for(item.get('text', '')))
               for i, item in enumerate(source_items) if isinstance(item, dict) and isinstance(item.get('text'), str)
               and warnings_for(item['text'])]
    from .memory_temporal import available
    if not available(pipeline.conn):
        if any(MARKER.search(str(item.get('text',''))) for item in source_items if isinstance(item,dict)):
            reports.append(dict(code='temporal_migration_required'))
        return output, {}, reports
    scope = json.loads(job['scope'])
    raw = [pipeline._raw_text(rid) for rid in json.loads(job['raw_ids'])]
    from .memory_gates import _source_stance, _statement_parts
    raw = sorted((r for r in raw if r['r'] == 'user'
                  and _source_stance(r)['support_scope'] == '可作主张依据'
                  and _statement_parts(r['t'])), key=lambda r: (r['ts'], r['id']))
    anchors = set()
    for item in source_items:
        if not isinstance(item, dict):
            continue
        obj = item.get('object')
        if (isinstance(obj, str) and 1 < len(obj) <= 40 and obj not in ('对象', '事情', '这件事', '状态', '偏好')
                and any(obj in r['t'] for r in raw)):
            anchors.add(obj)
    # A specific object and its literal parent can share one chain, but two
    # unrelated objects never acquire a common identity from proximity.
    anchors = {a for a in anchors if not any(b != a and b in a for b in anchors)}
    for row in pipeline.conn.execute('''SELECT m.meta FROM memories m JOIN memory_pipeline_owners o
        ON o.memory_id=m.id WHERE o.scope=? AND m.status='active' ORDER BY m.id DESC LIMIT 100''', (scope_key(scope),)):
        obj = _metadata(row).get('object')
        if isinstance(obj,str) and 1<len(obj)<=40 and any(obj in a for a in anchors) and any(obj in r['t'] for r in raw):
            anchors.add(obj)
    anchors = {a for a in anchors if not any(b != a and b in a for b in anchors)}
    plans, replaced = [], set()
    for anchor in sorted(anchors):
        groups = []
        previous = None
        for row in raw:
            signed = claim(row['t']) or ('陈述', row['t'])
            explicit = anchor in row['t']
            continuation = (previous is not None and not explicit and MARKER.search(row['t'])
                            and signed == previous['signed'] and 0 < row['ts'] - previous['ts'] <= 120)
            if not signed or not (explicit or continuation):
                previous = None
                continue
            # Competing anchors in one original make attribution ambiguous.
            if any(other != anchor and other in row['t'] for other in anchors):
                reports.append(dict(code='ambiguous_version_object', raw_id=row['id']))
                previous = None
                continue
            ref = dict(raw_id=row['id'], raw_ts=row['ts'], quote=row['t'])
            if len(row['t']) > 200:
                reports.append(dict(code='version_original_too_long', raw_id=row['id']))
                previous = None
                continue
            if (previous is not None and previous['signed'] == signed and MARKER.search(row['t'])
                    and 0 < row['ts'] - previous['ts'] <= 120 and len(groups[-1]['refs']) < 2):
                groups[-1]['refs'].append(ref)
            else:
                groups.append(dict(refs=[ref], signed=signed))
            previous = dict(signed=signed, ts=row['ts'])
        for dimension in {g['signed'][0] for g in groups}:
            selected = [g for g in groups if g['signed'][0] == dimension]
            key = 'extracted|' + anchor + '|' + dimension
            prior = _previous_version(pipeline, scope, key, anchor, selected[0]['refs'][0]['raw_ts'])
            previous_refs = _metadata(prior).get('source_refs', []) if prior else []
            has_pair = False
            for group in selected:
                refs = group['refs']
                if previous_refs and any(counterclaim(a['quote'], b['quote']) and anchor in a['quote'] and anchor in b['quote']
                                         for a in previous_refs for b in refs):
                    has_pair = True
                previous_refs = refs
            if not has_pair:
                if any(MARKER.search(r['quote']) for g in selected for r in g['refs']):
                    reports.append(dict(code='no_paired_opposition', object=anchor, dimension=dimension))
                continue
            for group in selected:
                ids = {r['raw_id'] for r in group['refs']}
                if ids & replaced:
                    raise ValueError('ambiguous source assigned to multiple version groups')
                replaced.update(ids)
                item = dict(text=_body(group['refs']), kind='preference' if dimension in ('口味','喜好') else 'fact', importance=.5,
                            object=anchor, action='', topic=anchor+('偏好' if dimension in ('口味','喜好') else '状态'), scene='',
                            source_refs=group['refs'], links=[])
                plans.append((item, dict(fact_key=key, object_anchor=anchor, dimension=dimension)))
    if not plans:
        return output, {}, reports
    kept = []
    for item in source_items:
        ids = {r.get('raw_id') for r in item.get('source_refs', []) if isinstance(r, dict)} if isinstance(item, dict) else set()
        if ids & replaced:
            if ids - replaced:
                raise ValueError('merged entry mixes version claims with unrelated sources; split it before extraction')
        else:
            kept.append(item)
    plans.sort(key=lambda p: p[0]['source_refs'][0]['raw_ts'])
    result = kept + [item for item, _ in plans]
    if len(result) > 12:
        raise ValueError('source-first split exceeds 12 entries; retry with a smaller batch')
    metadata = {len(kept)+i: spec for i, (_, spec) in enumerate(plans)}
    return {**output, 'items': result}, metadata, reports


def apply(pipeline, scope, mid, item, spec, job_id):
    """Caller owns the extraction transaction: endpoints, edges, audit and job commit together."""
    from .memory_temporal import TemporalFacts
    conn, now = pipeline.conn, pipeline.clock()
    facts = TemporalFacts(pipeline.memory, clock=pipeline.clock)
    refs = item['source_refs']
    old = _previous_version(pipeline, scope, spec['fact_key'],spec['object_anchor'],refs[0]['raw_ts'])
    oldmid = old['id'] if old else None
    old_refs = _metadata(old).get('source_refs', []) if old else []
    pair = next(((a, b) for a in old_refs for b in refs
                 if spec['object_anchor'] in a['quote'] and spec['object_anchor'] in b['quote']
                 and a['raw_ts'] < b['raw_ts'] and counterclaim(a['quote'], b['quote'])), None)
    if old and pair is None:
        pipeline._audit(scope, 'version_review', dict(job_id=job_id, memory_id=mid,
                                                    reason_code='no_paired_opposition', **spec))
        return None
    if old and old['link_id'] is None:
        oldstate=conn.execute("INSERT INTO memory_links(a_id,b_id,relation,why,created_at,t_valid,t_created) VALUES(?,?,'事实版本',?,?,?,?)",
                             (oldmid,oldmid,'新配对到达；保留旧条目ID；原话时间（迁移缺省值）',now,old_refs[0]['raw_ts'],now)).lastrowid
        conn.execute('INSERT INTO memory_pipeline_link_meta(link_id,scope,source_refs,operator,updated_at) VALUES(?,?,?,?,?)',
                     (oldstate,scope_key(scope),json.dumps(old_refs,ensure_ascii=False),'extraction_versions',now))
        old['link_id']=oldstate
    # One internal state edge per endpoint. These are not transition edges;
    # they preserve the round-11 edge-ID and current-version retrieval contract.
    cur = conn.execute("INSERT INTO memory_links(a_id,b_id,relation,why,created_at,t_valid,t_created) VALUES(?,?,'事实版本',?,?,?,?)",
                       (mid, mid, '原话形成独立端点；t_valid 为原话时间（迁移缺省值）', now, refs[0]['raw_ts'], now))
    state_id = cur.lastrowid
    conn.execute('INSERT INTO memory_pipeline_link_meta(link_id,scope,source_refs,operator,updated_at) VALUES(?,?,?,?,?)',
                 (state_id, scope_key(scope), json.dumps(refs, ensure_ascii=False), 'extraction_versions', now))
    if old is None:
        ref = facts._source(scope, refs[0], raw_records(pipeline.memory.raw.root))
        conn.execute('INSERT INTO memory_fact_versions VALUES(?,?,?,?,?,?)',
                     (scope_key(scope), spec['fact_key'], mid, state_id, ref['raw_id'], json.dumps(ref, ensure_ascii=False)))
        return dict(memory_id=mid, state_link_id=state_id, transition_link_id=None)
    a, b = pair
    reason = '两处原话支持相反评价：旧原话「'+a['quote']+'」；新原话「'+b['quote']+'」。t_valid=新原话时间（迁移缺省值）'
    result = facts.supersede(dict(scope, request_id='extract:'+job_id+':'+str(mid), operator='extraction_versions',
                                 old_link_id=old['link_id'], new_link_id=state_id,
                                 old_memory_id=oldmid, new_memory_id=mid, fact_key=spec['fact_key'],
                                 stance_tag='transition', object_anchor=spec['object_anchor'],
                                 reason=reason, source_refs=[a,b], t_valid=b['raw_ts']), connection=conn)
    snapshot = dict(b)
    snapshot['version_support_refs'] = [facts._source(scope, ref, raw_records(pipeline.memory.raw.root))
                                        for ref in refs if ref['raw_id'] != b['raw_id']]
    conn.execute('UPDATE memory_fact_versions SET source_ref=? WHERE scope=? AND memory_id=?',
                 (json.dumps(snapshot, ensure_ascii=False),scope_key(scope),mid))
    edge = conn.execute("INSERT INTO memory_links(a_id,b_id,relation,why,created_at,t_valid,t_created,stance_tag,reason) VALUES(?,?,'supersede',?,?,?,?,'transition',?)",
                        (oldmid, mid, reason, now, b['raw_ts'], now, reason)).lastrowid
    conn.execute('INSERT INTO memory_pipeline_link_meta(link_id,scope,source_refs,operator,updated_at,direction) VALUES(?,?,?,?,?,?)',
                 (edge, scope_key(scope), json.dumps([a,b],ensure_ascii=False), 'extraction_versions', now,
                  json.dumps(dict(from_memory_id=oldmid,to_memory_id=mid),ensure_ascii=False)))
    prior = conn.execute("SELECT l.id FROM memory_links l JOIN memory_pipeline_link_meta p ON p.link_id=l.id WHERE p.scope=? AND p.operator='extraction_versions' AND l.relation='supersede' AND l.b_id=? AND l.id!=? AND l.superseded_by IS NULL",
                         (scope_key(scope),oldmid,edge)).fetchone()
    if prior:
        conn.execute('UPDATE memory_links SET weight=.25,superseded_by=?,t_invalid=?,t_expired=? WHERE id=?',
                     (edge,b['raw_ts'],now,prior['id']))
    result.update(transition_link_id=edge, target_memory_id=mid, valid_time_basis='原话时间（迁移缺省值）')
    pipeline._audit(scope, 'extraction_transition', dict(job_id=job_id, **result))
    return result
