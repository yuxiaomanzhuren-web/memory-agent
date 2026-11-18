"""Read-only source accounting, separate from relevance and claim licensing."""
from collections import defaultdict
from datetime import datetime,timezone,timedelta
import json,sqlite3
from types import SimpleNamespace
from .memory_evidence import _metadata,_verify_record,owns_memory,scope_from,scope_key,raw_session
from .turn_recall import _assigned_records

SUPPORT_RELATIONS={'支持','佐证','依据','派生自','supports','derived_from','evidence_for'}
INACTIVE={'removed','orphaned'}

def decode(value):
    try:return json.loads(value) if isinstance(value,str) else value
    except ValueError:return None

def mid(value):
    return int(value) if type(value) is int and value>0 or isinstance(value,str) and value.isascii() and value.isdigit() and int(value)>0 else None

def components(vertices,edges):
    """Iterative Kosaraju: long provenance chains must not hit recursion limits."""
    forward={v:set() for v in vertices};reverse={v:set() for v in vertices}
    for a,b in edges:
        if a in forward and b in forward:forward[a].add(b);reverse[b].add(a)
    seen=set();order=[]
    for start in sorted(vertices):
        if start in seen:continue
        stack=[(start,False)]
        while stack:
            node,done=stack.pop()
            if done:order.append(node);continue
            if node in seen:continue
            seen.add(node);stack.append((node,True))
            stack.extend((n,False) for n in sorted(forward[node],reverse=True) if n not in seen)
    seen=set();result=[]
    for start in reversed(order):
        if start in seen:continue
        group=[];stack=[start];seen.add(start)
        while stack:
            node=stack.pop();group.append(node)
            for other in reverse[node]:
                if other not in seen:seen.add(other);stack.append(other)
        result.append(sorted(group))
    return sorted(result)

def account(nodes,edges=(),weak_edges=(),*,scope=None):
    """Pure batch accounting. Unknown roots are never invented from memory IDs."""
    nodes=[{**n,'raw_ids':sorted(set(n.get('raw_ids',[])))} for n in nodes]
    by_id={n['node_id']:n for n in nodes};vertices=set(by_id)
    edges={(a,b) for a,b in edges if a in vertices and b in vertices}
    cycles=[g for g in components(vertices,edges) if len(g)>1 or (g[0],g[0]) in edges]
    roots={rid:rid for n in nodes for rid in n['raw_ids']}
    def find(x):
        while roots[x]!=x:roots[x]=roots[roots[x]];x=roots[x]
        return x
    def union(a,b):
        a,b=find(a),find(b)
        if a!=b:roots[max(a,b)]=min(a,b)
    for group in cycles:
        raw=sorted({rid for node in group for rid in by_id[node]['raw_ids']})
        for rid in raw[1:]:union(raw[0],rid)
    raw_members=defaultdict(list)
    for node in nodes:
        for rid in node['raw_ids']:raw_members[rid].append(node['node_id'])
    units=defaultdict(set)
    for rid in roots:units[find(rid)].add(rid)
    groups=[]
    for i,raw in enumerate(sorted(units.values(),key=lambda s:sorted(s)),1):
        members=[n['node_id'] for n in nodes if raw.intersection(n['raw_ids'])]
        cyclic=any(set(members).intersection(c) and raw.intersection({r for v in c for r in by_id[v]['raw_ids']}) for c in cycles)
        groups.append(dict(id='basis:'+str(i),raw_ids=sorted(raw),node_ids=members,cycle=cyclic,evidence_units=1))
    cycle_by_node={v for group in cycles for v in group}
    for node in nodes:
        shared=[rid for rid in node['raw_ids'] if len(raw_members[rid])>1]
        duplicates=[rid for rid in shared if raw_members[rid][0]!=node['node_id']]
        node['same_source']=duplicates
        node['same_source_with']=sorted({v for rid in shared for v in raw_members[rid] if v!=node['node_id']})
        node['evidence_status']=('non_raw_origin' if node.get('origin_kind')=='daybook' else 'untraced') if not node['raw_ids'] else 'cyclic_dependency' if node['node_id'] in cycle_by_node else 'same_source' if duplicates else 'source_traced'
        node['evidence_vote_weight']=None if not node['raw_ids'] else sum(1/len(g['node_ids']) for g in groups if node['node_id'] in g['node_ids'])
    weak={(a,b) for a,b in weak_edges if a in vertices and b in vertices and a!=b}
    weak_groups=[g for g in components(vertices,weak|{(b,a) for a,b in weak}) if len(g)>1]
    same=[dict(raw_id=rid,node_ids=members) for rid,members in sorted(raw_members.items()) if len(members)>1]
    messages=[]
    for group in cycles:
        traced=any(by_id[v]['raw_ids'] for v in group)
        messages.append((f'这 {len(group)} 条互为依据，整体按一份来源算；内容和原话保留。' if len(group)>1 else '这条把自己当依据，不补成新证据。')
                        if traced else f'这 {len(group)} 条有循环依赖但来源未核实，暂不能核算份数。')
    for group in same:messages.append(f"这 {len(group['node_ids'])} 条出自同一句原话，我只按一份算（{group['raw_id']}）。")
    if weak_groups:messages.append(f'另有 {len(weak_groups)} 个弱关联组，只标结构，不合并或降权。')
    non_raw=sum(not n['raw_ids'] and n.get('origin_kind')=='daybook' for n in nodes)
    unknown=sum(not n['raw_ids'] for n in nodes)-non_raw
    if unknown:messages.append(f'还有 {unknown} 条来源未核实，不拿条目编号凑证据份数。')
    if non_raw:messages.append(f'另有 {non_raw} 条日记投影，属于非 raw 来源，不充当用户原话份数。')
    return dict(schema=1,scope=scope,status='ok',nodes=nodes,raw_source_count=len(roots),
        independent_evidence_count=len(groups),groups=groups,cycles=cycles,
        same_source=same,same_source_count=sum(len(g['node_ids'])-1 for g in same),
        weak_groups=weak_groups,weak_edges=[list(e) for e in sorted(weak)],untraced_count=unknown,non_raw_count=non_raw,count_complete=unknown==0,
        directed_edges=[list(e) for e in sorted(edges)],
        rendered='\n'.join(messages),count_semantics='verified_source_units_not_claim_support')

def graph(conn,rows,scope):
    """Only explicit support directions; sorted a_id/b_id are not directions."""
    owned={r['id']:r for r in rows};edges=set();weak=set();ignored=0
    checked=set(owned);rejected=set()
    def same_owner(ident):
        if ident in checked:return True
        if ident is None or ident in rejected:return False
        row=conn.execute('SELECT * FROM memories WHERE id=?',(ident,)).fetchone()
        if row is not None and owns_memory(SimpleNamespace(store=SimpleNamespace(conn=conn)),row,scope):
            checked.add(ident);return True
        rejected.add(ident);return False
    for ident,row in owned.items():
        meta=_metadata(row)
        parents=meta.get('derived_from_memory_ids',[])
        if type(meta.get('derived_from_memory_id')) is int:parents=[*parents,meta['derived_from_memory_id']] if isinstance(parents,list) else [meta['derived_from_memory_id']]
        for parent in parents if isinstance(parents,list) else []:
            if same_owner(mid(parent)):edges.add(('memory:'+str(mid(parent)),'memory:'+str(ident)))
        for dep in meta.get('evidence_dependencies',[]) if isinstance(meta.get('evidence_dependencies'),list) else []:
            parent=mid(dep.get('memory_id')) if isinstance(dep,dict) else None
            if same_owner(parent):edges.add(('memory:'+str(parent),'memory:'+str(ident)))
    names={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'memory_links' not in names:return edges,weak,ignored
    side={r['link_id']:dict(r) for r in conn.execute('SELECT * FROM memory_pipeline_link_meta')} if 'memory_pipeline_link_meta' in names else {}
    for link in conn.execute('SELECT * FROM memory_links'):
        row=dict(link);a,b=mid(row.get('a_id')),mid(row.get('b_id'))
        if row.get('a_kind')!='memory' or row.get('b_kind')!='memory' or (a not in owned and b not in owned):continue
        extra=side.get(row['id'],{})
        if extra.get('state') in INACTIVE or row.get('stance_tag')=='orphaned':continue
        if extra and extra.get('scope')!=scope_key(scope):continue
        if row.get('relation') not in SUPPORT_RELATIONS:
            if a in owned and b in owned:weak.add(('memory:'+str(a),'memory:'+str(b)))
            continue
        if not same_owner(a) or not same_owner(b):continue
        direction=decode(extra.get('direction'))
        source=mid(direction.get('source_memory_id')) if isinstance(direction,dict) else None
        target=mid(direction.get('target_memory_id')) if isinstance(direction,dict) else None
        if source is not None and target is not None and {source,target}=={a,b} and extra.get('state')=='active':
            edges.add(('memory:'+str(source),'memory:'+str(target)))
        else:ignored+=1
    return edges,weak,ignored

def analyze(memory,entries,scope):
    scope=scope_from(scope);conn=memory.store.conn
    selected=[];rows={};specs={};issues=[]
    for entry in entries:
        if not isinstance(entry,dict):continue
        ident=mid(entry.get('memory_id')) or (mid(entry.get('id')) if entry.get('source_kind')!='raw' else None)
        if ident:
            row=conn.execute('SELECT * FROM memories WHERE id=?',(ident,)).fetchone()
            if row is None or not owns_memory(memory,row,scope):continue
            rows[ident]=dict(row);name='memory:'+str(ident)
            refs=entry.get('source_refs')
            if not refs and entry.get('raw_id') and entry.get('quote'):refs=[entry]
            if not refs:
                meta=_metadata(row);refs=meta.get('source_refs') or ([{k:meta[k] for k in ('raw_id','raw_ts','quote') if k in meta}] if meta.get('raw_id') else [])
        else:
            ref=entry if entry.get('raw_id') else (entry.get('source_refs') or [None])[0]
            if not isinstance(ref,dict) or not isinstance(ref.get('raw_id'),str):continue
            name='raw:'+ref['raw_id'];refs=[ref]
        refs=[r for r in refs if isinstance(r,dict) and isinstance(r.get('raw_id'),str)] if isinstance(refs,list) else []
        if name in specs:
            specs[name].extend(r for r in refs if r not in specs[name])
            continue
        origin=_metadata(rows[ident]).get('source_kind') if ident else entry.get('source_kind')
        selected.append(dict(node_id=name,memory_id=ident,visible_text=str(entry.get('text') or ''),raw_ids=[],origin_kind=origin or 'raw'))
        specs[name]=refs
    ids=sorted({r['raw_id'] for refs in specs.values() for r in refs})
    index=[]
    if ids:
        try:
            # ponytail: small evidence batches; avoid a global archive scan and never expand unseen neighbors.
            for start in range(0,len(ids),500):
                chunk=ids[start:start+500]
                index.extend(dict(r) for r in conn.execute('SELECT * FROM memory_pipeline_raw WHERE scope=? AND session=? AND raw_id IN ('+','.join('?'*len(chunk))+')',
                    (scope_key(scope),raw_session(scope),*chunk)))
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):raise
    records=_assigned_records(memory.raw.root,scope,index)
    sources={}
    for node in selected:
        for ref in specs[node['node_id']]:
            try:checked=_verify_record(scope,ref,records)
            except (ValueError,TypeError,KeyError):
                issues.append(dict(node_id=node['node_id'],reason='source_not_verified_in_scope'));continue
            rid=checked['raw_id'];node['raw_ids'].append(rid)
            source=sources.setdefault(rid,dict(raw_id=rid,quotes=[],node_ids=[]))
            quote={**checked,**{k:ref[k] for k in ('meta_only','historical','can_support_found','support_target','evidence_kind') if k in ref}}
            if quote not in source['quotes']:source['quotes'].append(quote)
            if node['node_id'] not in source['node_ids']:source['node_ids'].append(node['node_id'])
    edges,weak,ignored=graph(conn,list(rows.values()),scope)
    result=account(selected,edges,weak,scope=scope)
    result.update(sources=list(sources.values()),issues=issues,undirected_support_ignored=ignored,
                  dependency_edges=[list(e) for e in sorted(edges)],
                  count_complete=result['count_complete'] and not issues and not ignored,purpose='provenance_accounting')
    for group in result['same_source']:
        times=[q['raw_ts'] for q in sources[group['raw_id']]['quotes']]
        if times:group['time']=datetime.fromtimestamp(float(times[0]),timezone(timedelta(hours=8))).strftime('%H:%M')
    if result['same_source']:
        lines=[]
        for g in result['same_source']:lines.append(f"这 {len(g['node_ids'])} 条出自同一句话（{g.get('time','时间未核实')} 那条），我只按一份算。")
        result['rendered']='\n'.join(lines+[line for line in result['rendered'].splitlines() if '出自同一句原话' not in line])
    return result

def entries_from(data):
    entries=list(data.get('hits') or []) if isinstance(data.get('hits'),list) else []
    gates=[data.get('gate1')] if isinstance(data.get('gate1'),dict) else [data] if data.get('evidence') else []
    for gate in gates:
        if isinstance(gate.get('evidence'),dict):entries.append(gate['evidence'])
        entries.extend(gate.get('source_clues') or [])
        for version in gate.get('revision_history') or []:
            for end in ('old','new'):
                ref=version.get(end)
                if not isinstance(ref,dict):continue
                entries.append({**ref,'memory_id':ref.get('endpoint_id') if ref.get('endpoint_kind')=='memory' else None})
    if isinstance(data.get('gate2'),dict):entries.extend(data['gate2'].get('hits') or [])
    return entries

def annotate(memory,data,scope):
    entries=entries_from(data)
    if not entries:return data
    with memory.lock,memory.store._lock:report=analyze(memory,entries,scope)
    out={**data,'evidence_independence':report}
    lookup={n['node_id']:n for n in report['nodes']}
    if isinstance(out.get('hits'),list):
        out['hits']=[{**h,'evidence_accounting':lookup.get('memory:'+str(h.get('memory_id') or h.get('id')),lookup.get('raw:'+str(h.get('raw_id') or h.get('id'))))} for h in out['hits']]
    if report['cycles']:
        print('[cycle_detected] '+json.dumps(dict(cycle_sizes=[len(g) for g in report['cycles']],item_count=len(report['nodes']),
            raw_source_count=report['raw_source_count'],evidence_units=report['independent_evidence_count']),ensure_ascii=False),flush=True)
    return out
