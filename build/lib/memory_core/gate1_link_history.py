"""Read existing supersede edges as context, never as source evidence or a write."""
import re
from .memory_evidence import _verify_record, scope_from, scope_key
from .source_provenance import rank
from .turn_recall import _tokens, _excerpt


def person_frame(text, common):
    """Mechanical roles only; ambiguous third-person references are not paired."""
    value = re.sub(r'^(?:对不起|抱歉|其实|不过|反倒|后来|之前|现在|刚才|纠正一下)[，,\s]*', '', text.strip())
    speaker, target = 'user', None
    speech = re.match(r'([我你您他她它])(?:不该|不应该|不应|曾经|之前|刚才|后来|已经|又|也|还|当时|现在|本来|不再|没)?(?:说|叫|骂|认为|觉得)([\s\S]+)', value)
    if speech:
        who, value = speech.groups()
        speaker = {'我':'user','你':'assistant','您':'assistant'}.get(who)
        if speaker is None:
            return None
        value = value.lstrip('过了：:，, \t“”「」『』\"')
    pronoun = re.match(r'([我你您他她它])', value)
    if pronoun:
        target = {'我':'user','你':'assistant','您':'assistant'}.get(pronoun[1])
        return (speaker, target) if target else None
    named = re.match(r'([\w\u3400-\u9fff]{2,12}?)(?=是|很|真|太|不|没|好|坏|喜欢|讨厌)', value)
    if named:
        return speaker, 'entity:'+named[1]
    # The live sample starts with a vocative, then explicitly addresses "你".
    if any(re.match(re.escape(word)+r'[\s，,！!。]', value) for word in common) and re.search(r'[你您]', value):
        return speaker, 'assistant'
    if re.search(r'[我你您他她它]', value):
        return None
    return (speaker, 'object:'+max(common, key=lambda word:(len(word),word))) if common else None


def _pair_reason(a, b):
    common = (_tokens(a['quote']) & _tokens(b['quote'])) - {'其实','说过','不该','不是','就是','喜欢','讨厌','觉得','认为','之前','现在'}
    common = {word for word in common if len(word) >= 2 and any(ch not in '我你您他她它的是了说不' for ch in word)}
    first, second = person_frame(a['quote'], common), person_frame(b['quote'], common)
    if first is None or second is None:
        return 'person_unresolved'
    if first != second:
        return 'person_mismatch'
    if not common:
        return 'object_mismatch'
    same_raw=a.get('raw_id') and a.get('raw_id')==b.get('raw_id')
    ordered_spans=(same_raw and a.get('quote_end') is not None and b.get('quote_start') is not None
                   and a['quote_end']<=b['quote_start'])
    if a['raw_ts'] > b['raw_ts'] or a['raw_ts']==b['raw_ts'] and not ordered_spans:
        return 'time_order_mismatch'
    return None


def _located(ref, records, quote=None):
    quote=ref['quote'] if quote is None else quote
    text=records[ref['raw_id']]['t'];start=text.find(quote)
    unique=start>=0 and text.find(quote,start+1)<0
    return {**ref,'quote':quote,'text':quote,'ts':ref['raw_ts'],
            'quote_start':start if unique else None,'quote_end':start+len(quote) if unique else None,
            'quote_index_unit':'unicode_code_points'}


def _source_key(ref):
    return ref['raw_id'],ref.get('quote_start'),ref.get('quote_end'),ref['quote']


def attach_history(gates, data, result, context):
    from .memory_gates import MemoryGates, _date, _statement_parts
    scope, records, rows = scope_from(data), context['records'], context['rows']
    seed = result['evidence']['raw_id']
    conn = gates.memory.store.conn
    with gates.memory.lock, gates.memory.store._lock:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'memory_links' not in tables:
            return result
        # ponytail: scan the small supersede subset; add a relation index if this becomes large.
        cursor = conn.execute("SELECT * FROM memory_links WHERE relation='supersede' ORDER BY created_at,id")
        names = [col[0] for col in cursor.description]
        links = [dict(zip(names, row)) for row in cursor]
        sidecars = {}
        if 'memory_pipeline_link_meta' in tables:
            sidecars = {row[0]:(row[1],row[2]) for row in conn.execute('SELECT link_id,scope,state FROM memory_pipeline_link_meta')}
        versions = {}
        if 'memory_fact_versions' in tables:
            versions = {row[0]:row[1] for row in conn.execute('SELECT memory_id,source_raw_id FROM memory_fact_versions WHERE scope=?', (scope_key(scope),))}
    owned = {str(row['id']):row for row in rows}

    def endpoint(kind, ident):
        if kind == 'raw':
            rec = records.get(str(ident))
            refs = [] if rec is None else [_verify_record(scope, dict(raw_id=rec['id'],quote=rec['t']),records,from_source=True)]
        elif kind == 'memory' and str(ident) in owned:
            refs = gates._refs(owned[str(ident)],scope,records)
            primary = versions.get(int(ident))
            if primary:
                refs = [ref for ref in refs if ref['raw_id']==primary]
            elif any(ref['raw_id']==seed for ref in refs):
                refs = [ref for ref in refs if ref['raw_id']==seed]
        else:
            return None
        for ref in refs:
            rec = records.get(ref['raw_id'])
            if rec is None or ref['role'] != 'user' or rank(ref)!=2 or ref['stance']!='他说':
                continue
            # Original full-record question exclusion and source checks remain in force.
            if not any(part.rstrip('。！？?!；;\n').strip() in ref['quote'] for part in _statement_parts(rec['t'])):
                continue
            return _located({**ref,'time':_date(ref['raw_ts']),
                    'endpoint_kind':kind,'endpoint_id':ident,'quote_truncated':len(ref['quote'])>800},
                    records,_excerpt(ref['quote'],sorted(_tokens(data['text'])),800))
        return None

    pairs, diagnostics = [], []
    for link in links:
        side = sidecars.get(link['id'])
        if side is not None and side[0]!=scope_key(scope):
            continue  # Hide a foreign relation as well as its endpoint, not the local evidence.
        a, b = endpoint(link['a_kind'],link['a_id']), endpoint(link['b_kind'],link['b_id'])
        incident = any(ref and ref['raw_id']==seed for ref in (a,b))
        if not a or not b:
            continue  # An unavailable endpoint must not reveal the relationship or erase the seed.
        if side is not None and side[1]!='active':
            if incident:
                diagnostics.append(dict(link_id=link['id'],reason='removed_or_foreign_link'))
            continue
        if any(link.get(field) is not None and link[field]>context['now'] for field in ('t_valid','t_created')):
            continue
        # Do not let excerpting cut off a speaker or reverse a pronoun's referent.
        problem = _pair_reason(a,b) if a['raw_id']==b['raw_id'] else _pair_reason(
            {**a,'quote':records[a['raw_id']]['t']},{**b,'quote':records[b['raw_id']]['t']})
        if problem:
            if incident:
                diagnostics.append(dict(link_id=link['id'],reason=problem))
            continue
        pairs.append((link,a,b))
    # Only directly incident versions; do not inject a third-hop neighbor's body.
    incident_pairs=[p for p in pairs if seed in (p[1]['raw_id'],p[2]['raw_id'])]
    selected=incident_pairs[:8]
    if not selected:
        if diagnostics:
            result['revision_diagnostics']=diagnostics
        return result
    historical = {_source_key(a) for _,a,_ in pairs}
    checker=MemoryGates(gates.memory,clock=lambda:context['now'])
    checks={}
    for _,a,b in selected:
        for ref in (a,b):
            rid=ref['raw_id'];key=_source_key(ref)
            if key not in checks:
                # Verify this displayed utterance, not another clause sharing the same raw ID.
                view={**records,rid:{**records[rid],'t':ref['quote']}}
                checked=checker._one(data,loaded=(rows,view),only_raw_id=rid)
                checks[key]=dict(state=checked['state'],missing=checked.get('missing'),
                                 meta_only=bool(checked.get('meta_only')),searched=checked.get('searched'))
    history=[];seen={}
    for link,a,b in sorted(selected,key=lambda pair:(pair[2]['raw_ts'],pair[0]['id'])):
        pair_key=(_source_key(a),_source_key(b))
        if pair_key in seen:
            seen[pair_key]['link_ids'].append(link['id']);continue
        old={**a,'historical':True,'status':'superseded','t_invalid':link.get('t_invalid'),'check':checks[_source_key(a)]}
        new={**b,'historical':_source_key(b) in historical,'status':'superseded' if _source_key(b) in historical else 'current',
             'check':checks[_source_key(b)]}
        for endpoint_ref in (old,new):
            endpoint_ref['can_support_found']=not endpoint_ref['historical'] and not endpoint_ref['check']['meta_only'] and endpoint_ref['check']['state']=='found'
        short_a, short_b = a['quote'][:160], b['quote'][:160]
        line=(f"你 {_date(a['raw_ts'])[11:16]} 先说过『{short_a}』（历史版本），"
              f"{_date(b['raw_ts'])[11:16]} 后来又说『{short_b}』；连线登记为“后来改口”。"+
              ('后句也有后续版本，不当作现行。' if new['historical'] else '我参考后来的表述，旧句只作历史版本。')+
              '两条原话各自核验，连线不证明哪一方为真。')
        item=dict(link_id=link['id'],link_ids=[link['id']],relation='supersede',reason=link.get('reason') or link.get('why',''),
                  old=old,new=new,context_only=True,person_check='aligned',same_source=a['raw_id']==b['raw_id'],rendered=line)
        history.append(item);seen[pair_key]=item
    base_state=result['state']
    primary=_located(result['evidence'],records)
    seed_refs=[ref for _,a,b in pairs for ref in (a,b) if ref['raw_id']==seed]
    exact=[ref for ref in seed_refs if _source_key(ref)==_source_key(primary)]
    seed_historical=(_source_key(primary) in historical if exact else
                     all(_source_key(ref) in historical for ref in seed_refs))
    mixed=not exact and any(_source_key(ref) in historical for ref in seed_refs) and not seed_historical
    if (seed_historical or mixed) and base_state=='found' and not result.get('meta_only'):
        result['state']='suspected'
        result['missing']='命中的是已被后来原话取代的历史版本，不能当作现行事实'
        result['rendered']=result['rendered'].replace('[found]','[suspected]',1)
    result['evidence']['historical']=seed_historical
    result['evidence']['can_support_current_fact']=not seed_historical and not mixed and result['state']=='found' and not result.get('meta_only')
    result['version_history']=history
    result['revision_base_state']=base_state
    result['revision_history_truncated']=len(incident_pairs)>len(selected)
    if diagnostics:
        result['revision_diagnostics']=diagnostics
    context_text=' '.join(item['rendered'] for item in history)
    # Keep the bridge's existing suspected contract: exactly three parts, ending with this sentence.
    if result['state']=='suspected':
        if base_state!='suspected':
            result['parts']=['命中历史原话：'+result['evidence']['quote'],'缺什么：'+result['missing'],'所以我只能说到这儿']
        result['parts'][0]+='；'+context_text
    else:
        result.setdefault('parts',[]).append(context_text)
    if result['state']=='suspected' and base_state!='suspected':
        result['rendered']='[suspected] '+'；'.join(result['parts'])
    else:
        result['rendered']+=' '+context_text
    # Existing source-clue transport already labels/injects these references and authorizes quoting.
    # Added endpoints authorize only that an utterance exists, never the truth of its content.
    clues=result.setdefault('source_clues',[])
    cited={(seed,result['evidence']['quote'])} | {(ref['raw_id'],ref['quote']) for ref in clues}
    for item in history:
        for ref in (item['old'],item['new']):
            quote=ref['quote'][:160]
            if (ref['raw_id'],quote) not in cited:
                clues.append({**_located(ref,records,quote),'meta_only':True,'support_target':'utterance_only','evidence_kind':'version_history',
                              'same_source_group':ref['raw_id']})
                cited.add((ref['raw_id'],quote))
    return result
