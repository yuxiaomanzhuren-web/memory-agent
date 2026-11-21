"""Read-only exact-text / dependency audit. Never constructs a Store or a service."""
from __future__ import annotations
import json
import sqlite3
import hashlib
from contextlib import closing
from pathlib import Path
from datetime import datetime,timezone,timedelta

LOG_TABLES={'memory_delete_log'}
ID_KEYS={'memory_id','memoryId','source_memory_id','superseded_memory_id'}
RAW_KEYS={'raw_id','source_raw_id','archive_raw_id'}
TEXT_KEYS={'text','t','quote','why','reason','content','body','summary','payload','result','source_ref',
           'source_refs','meta','direction','findings','small','confirmation','detail','tokens','input',
           'object','action','topic','scene','event_key','stance_reason','word','text_excerpt'}
TEXT_KEYS.update({'name','profile','aliases'})
TEXT_KEYS.update({'claim','query','gap_summary','event_json','receipt'})
ID_KEYS.add('evidence_memory_id')
RAW_KEYS.add('evidence_raw_id')
SIDEREFS={'memory_pipeline_outbox','memory_pipeline_turn_starts','citation_sources','memory_citation_sources'}
REDACTED='[已删除]'

def readonly(file):
    conn=sqlite3.connect(Path(file).resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN') # Consistent read snapshot; still no schema or data writes.
    return conn

def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

def q(name):
    return '"'+name.replace('"','""')+'"'

def decode(value):
    if isinstance(value,str):
        try:return json.loads(value)
        except (ValueError,TypeError):pass
    return value

def refs_in(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if key in RAW_KEYS and isinstance(item,str):yield item
            yield from refs_in(item)
    elif isinstance(value,list):
        for item in value:yield from refs_in(item)

def scope_matches(value,target):
    wanted=decode(target.get('source',{}).get('scope'))
    declared=decode(value.get('scope')) if isinstance(value,dict) else None
    declared=declared if isinstance(declared,dict) else value
    if isinstance(wanted,dict) and isinstance(declared,dict) and all(str(declared.get(k,''))==str(wanted.get(k,'')) for k in ('bot_id','persona','conv_key')):
        return True
    source=target.get('source',{})
    if isinstance(declared,dict) and all(k in declared for k in ('bot_id','persona','conv_key')):
        if scope_digest(declared)==source.get('scope_digest'):return True
    session=value.get('s',value.get('session')) if isinstance(value,dict) else None
    if isinstance(session,str) and hashlib.sha256(session.encode()).hexdigest()==source.get('session_digest'):return True
    return isinstance(wanted,dict) and session==str(wanted.get('bot_id'))+'|'+str(wanted.get('conv_key'))

def scope_digest(scope):
    value={k:str(scope[k]) for k in ('bot_id','persona','conv_key')}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def references(value,target):
    if isinstance(value,str):
        parsed=decode(value)
        return references(parsed,target) if not isinstance(parsed,str) else value in target.get('reference_ids',target['raw_ids'])
    if isinstance(value,list):
        return any(references(item,target) for item in value)
    if not isinstance(value,dict):return False
    for key,item in value.items():
        if key in ID_KEYS and item==target['memory_id']:return True
        if key in ('a_id','b_id') and item==target['memory_id'] and value.get(key[0]+'_kind','memory')=='memory':return True
        if key in ('link_id','superseded_by') and item in target['link_ids']:return True
        if key in RAW_KEYS and item in target.get('reference_ids',target['raw_ids']):return True
        if key in ('raw_ids','memory_ids') and isinstance(decode(item),list):
            allowed=target.get('reference_ids',target['raw_ids']) if key=='raw_ids' else {target['memory_id']}
            if any(isinstance(v,(str,int)) and v in allowed for v in decode(item)):return True
        if key in ('message_id','messageId') and str(item) in target['message_ids'] and scope_matches(value,target):return True
        if references(item,target):return True
    return False

def strings(value):
    if isinstance(value,str):
        parsed=decode(value)
        if isinstance(parsed,(dict,list)):yield from strings(parsed)
        else:yield value
    elif isinstance(value,dict):
        for value in value.values():yield from strings(value)
    elif isinstance(value,list):
        for value in value:yield from strings(value)

def text_length(row):
    def visit(value,content=False):
        parsed=decode(value)
        if isinstance(parsed,dict):return sum(visit(v,k in TEXT_KEYS) for k,v in parsed.items())
        if isinstance(parsed,list):return sum(visit(v,content) for v in parsed)
        return len(parsed) if content and isinstance(parsed,str) and parsed!=REDACTED else 0
    return visit(row)

def related_text_length(value,target):
    parsed=decode(value)
    if isinstance(parsed,list):return sum(related_text_length(v,target) for v in parsed)
    if not isinstance(parsed,dict):return 0
    own=references(parsed,target)
    length=0
    for key,value in parsed.items():
        child=decode(value)
        if isinstance(child,(dict,list)):length+=related_text_length(child,target)
        elif own and key in TEXT_KEYS and isinstance(child,str) and child!=REDACTED:length+=len(child)
    return length

def literal_hits(value,target):
    text='\n'.join(strings(value))
    return [needle for needle in target['needles'] if needle and any(v in text for v in variants(needle))]

def variants(needle):
    return {needle,json.dumps(needle,ensure_ascii=True)[1:-1],json.dumps(needle,ensure_ascii=False)[1:-1]}

def describe_target(conn,memory_id,raw_root,needle=None):
    names=tables(conn);row=conn.execute('SELECT * FROM memories WHERE id=?',(int(memory_id),)).fetchone()
    log=conn.execute('SELECT mode,source FROM memory_delete_log WHERE memory_id=? ORDER BY id DESC LIMIT 1',(int(memory_id),)).fetchone() if 'memory_delete_log' in names else None
    saved=decode(log['source']) if log else {}
    if row:
        row=dict(row);meta=decode(row.get('meta')) or {}
        refs=meta.get('source_refs') or ([{k:meta[k] for k in ('raw_id','quote') if k in meta}] if meta.get('raw_id') else [])
        source={'scope':meta.get('pipeline_scope'),'refs':refs}
        if log and log['mode'] in ('purge_pending','purge_completed'):
            source['raw_clean_sha256']=saved.get('raw_clean_sha256',{})
        text=row.get('text','')
    else:
        source=saved
        text='';refs=source.get('refs',[])
    target={'memory_id':int(memory_id),'raw_ids':set(refs_in(source)),'message_ids':set(),
            'needles':set([text] if text else []),'refs':refs,'source':source,'link_ids':set(),'raw_records':{},
            'body_present':row is not None,'shared':[]}
    target['entity_ids']=set(source.get('entity_ids',[]))
    if 'memory_entities' in names:
        target['entity_ids'].update(r[0] for r in conn.execute('SELECT entity_id FROM memory_entities WHERE memory_id=?',(memory_id,)))
    target['shared_entities']=[]
    for eid in target['entity_ids']:
        others={r[0] for r in conn.execute('SELECT memory_id FROM memory_entities WHERE entity_id=? AND memory_id<>?',(eid,memory_id))}
        if 'relations' in names:
            others.update(r[0] for r in conn.execute('SELECT memory_id FROM relations WHERE (src_id=? OR dst_id=?) AND memory_id IS NOT NULL AND memory_id<>?',(eid,eid,memory_id)))
        if others:target['shared_entities'].append({'entity_id':eid,'memory_ids':sorted(others)})
    target['link_ids'].update(source.get('link_ids',[]))
    target['message_ids'].update(source.get('message_ids',[]))
    if not source.get('scope') and 'memory_pipeline_owners' in names:
        owner=conn.execute('SELECT scope FROM memory_pipeline_owners WHERE memory_id=?',(memory_id,)).fetchone()
        if owner:source['scope']=decode(owner[0])
    if needle:target['needles'].add(str(needle))
    for ref in refs:
        if isinstance(ref,dict) and ref.get('quote'):target['needles'].add(ref['quote'])
    if 'memory_links' in names:
        target['link_ids'].update(r[0] for r in conn.execute(
            "SELECT id FROM memory_links WHERE (a_kind='memory' AND a_id=?) OR (b_kind='memory' AND b_id=?)",(memory_id,memory_id)))
    if raw_root and Path(raw_root).exists():
        for file in sorted(Path(raw_root).rglob('*.jsonl')):
            if not file.resolve().is_relative_to(Path(raw_root).resolve()):raise ValueError('raw_path_outside_root')
            with file.open('rb') as stream:
                while True:
                    offset=stream.tell();blob=stream.readline()
                    if not blob:break
                    try:rec=json.loads(blob)
                    except (ValueError,UnicodeError):continue
                    rid=rec.get('id')
                    if rid in target['raw_ids']:
                        target['raw_records'].setdefault(rid,[]).append({'record':rec,'file':str(file.resolve()),'offset':offset,'size':len(blob)})
                        if rec.get('message_id') is not None:target['message_ids'].add(str(rec['message_id']))
                        for ref in refs:
                            already_clean=source.get('raw_clean_sha256',{}).get(rid)==hashlib.sha256(blob).hexdigest()
                            if not already_clean and not source.get('purged') and ref.get('raw_id')==rid and 'start' in ref and 'end' in ref:
                                target['needles'].add(rec.get('t','')[ref['start']:ref['end']])
    for other in conn.execute('SELECT id,text,meta FROM memories WHERE id<>?',(memory_id,)):
        overlap=target['raw_ids'].intersection(refs_in(decode(other['meta'])))
        if overlap or literal_hits(dict(other),target):target['shared'].append({'memory_id':other['id'],'raw_ids':sorted(overlap),
            'reason':'shared_raw' if overlap else 'same_content_in_another_memory'})
    target['needles'].discard('')
    citation_ids=set(source.get('citation_ids',[]))
    scope=decode(source.get('scope'))
    if isinstance(scope,dict) and all(scope.get(k) for k in ('bot_id','persona','conv_key')):
        key=json.dumps([str(scope['bot_id']),scope['persona'],scope['conv_key']],ensure_ascii=False,separators=(',',':'))
        for copies in target['raw_records'].values():
            for copy in copies:
                rec=copy['record']
                if rec.get('message_id') is not None:
                    citation_ids.add('transcript:'+hashlib.sha256((key+'\n'+rec['r']+'\n'+str(rec['message_id'])).encode()).hexdigest())
    target['citation_ids']=citation_ids
    target['reference_ids']=target['raw_ids']|citation_ids
    return target

def row_matches(table,row,target,trusted_ids=True):
    if table in LOG_TABLES:return bool(literal_hits(row,target))
    return ((trusted_ids and table=='memories' and row.get('id')==target['memory_id'])
            or trusted_ids and table=='entities' and row.get('id') in target['entity_ids']
            or trusted_ids and references(row,target) or bool(literal_hits(row,target)))

def scan_database(conn,label,target):
    surfaces=[]
    for table in sorted(tables(conn)):
        if table.startswith('sqlite_') or table.startswith('memories_fts_'):continue
        for number,row in enumerate(conn.execute('SELECT * FROM '+q(table))):
            data=dict(row)
            trusted=label=='backend' or table in SIDEREFS
            if not row_matches(table,data,target,trusted):continue
            hits=literal_hits(data,target)
            length=text_length(data) if table in ('memories','entities') else related_text_length(data,target)
            # Source indexes, IDs and vector bytes are dependencies; they are not plaintext.
            chars=max([len(hit) for hit in hits]+[length if trusted and references(data,target) or table in ('memories','entities') else 0])
            surfaces.append({'store':label,'table':table,'row':data.get('id',data.get('memory_id',data.get('raw_id',number))),
                             'recoverable_chars':chars,'kind':'plaintext' if chars else 'reference',
                             'opaque_bytes':sum(len(v) for v in data.values() if isinstance(v,bytes)),
                             'columns':sorted(key for key,value in data.items() if isinstance(value,str) and (literal_hits(decode(value),target) or key in TEXT_KEYS and references(data,target))),
                             'shared_source':table=='memories' and data.get('id')!=target['memory_id']})
    return surfaces

def eligible_files(root):
    root=Path(root).resolve()
    if root.is_file():yield root;return
    for file in sorted(root.rglob('*')):
        if not file.is_file():continue
        # Browser storage is not an application-memory store. Declare it outside scope.
        if 'browser-profile' in file.relative_to(root).parts:continue
        if '.bak' in file.name or 'backup' in file.name.lower():continue
        if not file.resolve().is_relative_to(root):raise ValueError('managed_file_outside_root')
        if file.suffix.lower() in {'.json','.jsonl','.log','.txt','.yaml','.yml','.sqlite','.db'} or any(s in file.name for s in ('.log.','.json.','.sqlite.')):
            if file.name.endswith(('-wal','-shm')):continue
            yield file

def document(line):
    """Return a log prefix, decoded JSON (if present), and suffix."""
    try:return '',json.loads(line),''
    except ValueError:pass
    starts=[i for ch in ('{','[') if (i:=line.find(ch))>=0]
    for start in sorted(starts):
        try:
            value,end=json.JSONDecoder().raw_decode(line[start:])
            if isinstance(value,(dict,list)):return line[:start],value,line[start+end:]
        except ValueError:pass
    return '',line,''

def file_documents(file,text):
    if file.suffix.lower()=='.json':
        yield 1,decode(text)
    else:
        for number,line in enumerate(text.splitlines(),1):
            if line.strip():yield number,document(line)[1]

def audit(conn,memory_id,raw_root=None,bridge_states=(),needle=None,target=None,extra_connections=None):
    target=target or describe_target(conn,memory_id,raw_root,needle)
    surfaces=scan_database(conn,'backend',target);errors=[];files_checked=[]
    if raw_root:
        if not Path(raw_root).is_dir():errors.append({'store':'raw','reason':'root_missing'})
        else:
            for file in Path(raw_root).rglob('*.jsonl'):
                files_checked.append(str(file))
                for line_no,line in enumerate(file.read_text('utf8').splitlines(),1):
                    parsed=decode(line)
                    if not isinstance(parsed,dict):
                        errors.append({'store':str(file),'line':line_no,'reason':'invalid_jsonl'});continue
                    owned=parsed.get('id') in target['raw_ids'];hits=literal_hits(parsed,target)
                    if owned or hits:
                        surfaces.append({'store':'raw','file':str(file),'row':parsed.get('id',line_no),
                            'recoverable_chars':max(map(len,hits),default=0) if target['needles'] else len(parsed.get('t','')) if owned else 0,
                            'kind':'plaintext' if hits else 'reference'})
    for root in bridge_states:
        if not Path(root).exists():errors.append({'store':str(root),'reason':'root_missing'});continue
        for file in eligible_files(root):
            files_checked.append(str(file))
            if file.stat().st_size>30_000_000:
                errors.append({'store':str(file),'reason':'file_over_30MB'});continue
            try:
                if file.read_bytes()[:16]==b'SQLite format 3\x00':
                    if extra_connections and str(file.resolve()) in extra_connections:
                        surfaces.extend(scan_database(extra_connections[str(file.resolve())],str(file),target))
                    else:
                        with closing(readonly(file)) as extra:
                            surfaces.extend(scan_database(extra,str(file),target))
                    continue
                text=file.read_text('utf-8-sig')
                lines=text.splitlines()
                for number,parsed in file_documents(file,text):
                    if file.suffix.lower() in ('.json','.jsonl') and not isinstance(parsed,(dict,list)):
                        errors.append({'store':str(file),'line':number,'reason':'invalid_structured_file'})
                    original=text if file.suffix.lower()=='.json' else lines[number-1]
                    hits=list(set(literal_hits(parsed,target)+literal_hits(original,target)))
                    if hits or references(parsed,target):
                        surfaces.append({'store':'bridge_file','file':str(file),'row':number,
                            'recoverable_chars':max(map(len,hits),default=0) or related_text_length(parsed,target),
                            'kind':'plaintext' if hits else 'reference','backup':'.bak' in file.name or 'backup' in file.name})
            except (OSError,UnicodeError,sqlite3.Error) as exc:
                errors.append({'store':str(file),'reason':type(exc).__name__})
    plaintext=[s for s in surfaces if s['recoverable_chars']>0]
    opaque=[s for s in surfaces if s.get('opaque_bytes',0)>0]
    return {'memory_id':int(memory_id),'mode':'read_only','body_present':bool(conn.execute('SELECT 1 FROM memories WHERE id=?',(memory_id,)).fetchone()),
            'as_of':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
            'surfaces':surfaces,'plaintext_surfaces':len({(s['store'],s.get('table'),s.get('file')) for s in plaintext}),
            'plaintext_rows':len(plaintext),'recoverable_chars_total':sum(s['recoverable_chars'] for s in plaintext),
            'length_unit':'unicode_codepoints_in_associated_payloads; copies_and_indexes_not_deduplicated',
            'content_probe_available':bool(target['needles']),'opaque_surfaces':len(opaque),'shared_raw':target['shared'],'shared_entities':target['shared_entities'],'errors':errors,
            'files_checked':len(files_checked),'query_only':bool(conn.execute('PRAGMA query_only').fetchone()[0]),
            'original_memory_probe_available':target['body_present'] or bool(needle),
            'verified_zero_managed':not plaintext and not opaque and not errors and bool(target['needles']) and (target['body_present'] or bool(needle)),
            'outside_scope':['浏览器缓存','DSH/QQ及模型服务端会话','未纳管备份和其它目录','截图/音视频等非文本附件','磁盘取证/SSD/文件系统快照'],
            'method':'exact_text_and_explicit_ids; no semantic paraphrase claim'}
