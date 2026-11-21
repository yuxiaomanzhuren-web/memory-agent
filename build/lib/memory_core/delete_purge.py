"""Offline forward-only cleanup. No original-content backups are written."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import uuid
from .delete_audit import (audit,describe_target,eligible_files,tables,q,decode,references,
                          literal_hits,variants,row_matches,TEXT_KEYS,SIDEREFS,REDACTED)
from .delete_audit import scope_matches
from .delete_audit import document
from .memory_delete import DeleteError,OUTSIDE,schema,descriptor,hard_rows,write_log,actor_id,validated_memory_id

def assert_offline(ports=(8781,3100)):
    active=[]
    for port in ports:
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.25):active.append(port)
        except OSError:pass
    if active:raise DeleteError('services_must_be_stopped',details={'active_ports':active})

def scrub(value,target):
    if isinstance(value,str):
        parsed=decode(value)
        if isinstance(parsed,(dict,list)):
            cleaned=scrub(parsed,target)
            return json.dumps(cleaned,ensure_ascii=False,separators=(',',':')) if cleaned!=parsed else value
        for needle in sorted(target['needles'],key=len,reverse=True):
            for variant in variants(needle):value=value.replace(variant,REDACTED)
        return value
    if isinstance(value,list):return [scrub(v,target) for v in value]
    if not isinstance(value,dict):return value
    direct=any((k in ('raw_id','archive_raw_id','source_raw_id') and v in target.get('reference_ids',target['raw_ids']))
               or (k=='memory_id' and v==target['memory_id'] and scope_matches(value,target))
               or (k in ('messageId','message_id') and str(v) in target['message_ids'] and scope_matches(value,target))
               for k,v in value.items() if isinstance(v,(str,int)))
    result={}
    related=direct or references(value,target)
    for key,item in value.items():
        if related and key in TEXT_KEYS and isinstance(item,str) and not isinstance(decode(item),(dict,list)):
            result[key]=''
        else:result[key]=scrub(item,target)
    if direct:result['redacted']=True
    if direct and result.get('event')=='judge_decision':
        result.update(event='judge_redacted',verdict='redacted',final_bucket=None)
        result.pop('text_hash',None)
    return result

def clean_file(file,text,target):
    if file.suffix.lower()=='.json':
        value=decode(text);clean=scrub(value,target)
        return json.dumps(clean,ensure_ascii=False,indent=2) if clean!=value else text
    lines=[]
    for line in text.splitlines(keepends=True):
        prefix,value,suffix=document(line)
        clean=scrub(value,target)
        clean_prefix=scrub(prefix,target);clean_suffix=scrub(suffix,target)
        if clean==value and clean_prefix==prefix and clean_suffix==suffix:lines.append(line)
        elif isinstance(clean,(dict,list)):
            lines.append(clean_prefix+json.dumps(clean,ensure_ascii=False,separators=(',',':'))+clean_suffix+('\n' if line.endswith('\n') and not clean_suffix.endswith('\n') else ''))
        else:lines.append(clean)
    return ''.join(lines)

def clean_database(conn,target,*,backend=False):
    names=tables(conn)
    for table in sorted(names):
        if table.startswith('sqlite_') or table.startswith('memories_fts') or table in ('memory_delete_log','memories','vectors','memory_entities'):
            continue
        cols=[r[1] for r in conn.execute('PRAGMA table_info('+q(table)+')')]
        rows=list(conn.execute('SELECT rowid AS "__delete_rowid",* FROM '+q(table)))
        for row in rows:
            item=dict(row);rid=item.pop('__delete_rowid')
            if not row_matches(table,item,target,backend or table in SIDEREFS):continue
            changed={}
            for key,value in item.items():
                if isinstance(value,bytes) and references(item,target):
                    changed[key]=b'';continue
                if not isinstance(value,str):continue
                cleaned=scrub(value,target)
                # Direct row-level dependencies may contain partial copied material.
                if key in TEXT_KEYS and (backend or table in SIDEREFS) and references(item,target):
                    parsed=decode(value)
                    cleaned='{}' if isinstance(parsed,dict) else '[]' if isinstance(parsed,list) else ''
                if cleaned!=value:changed[key]=cleaned
            if table=='memory_pipeline_outbox':
                body=decode(item.get('body','{}'))
                cleaned=scrub(body,target)
                if isinstance(cleaned,dict):cleaned.update(redacted=True,text='')
                changed.update(body=json.dumps(cleaned,ensure_ascii=False,separators=(',',':')),state='redacted',last_error='')
            if table=='memory_pipeline_jobs':
                remaining=[r for r in (decode(item['raw_ids']) or []) if r not in target['raw_ids']]
                changed['raw_ids']=json.dumps(remaining)
                changed.update(error='',lease_token=None,lease_until=0)
                if not remaining:changed['status']='cancelled'
                elif item['status']=='running':changed.update(status='queued',next_at=0)
            if table=='memory_pipeline_raw' and item['raw_id'] in target['raw_ids']:
                changed['processed']=2
                tags=decode(item.get('source_tags','{}'))
                tags=tags if isinstance(tags,dict) else {}
                tags={k:('' if k in TEXT_KEYS else v) for k,v in tags.items()}
                changed['source_tags']=json.dumps({**tags,'redacted':True},ensure_ascii=False)
            changed={k:v for k,v in changed.items() if k in cols}
            if changed:conn.execute('UPDATE '+q(table)+' SET '+','.join(q(k)+'=?' for k in changed)+' WHERE rowid=?',(*changed.values(),rid))
    if not backend and 'memories' in names:
        # Legacy bridge memories use a different ID space; match text, never the backend ID.
        for row in list(conn.execute('SELECT * FROM memories')):
            if not literal_hits(dict(row),target):continue
            local=row['id']
            for table in ('memories_fts','vectors','memory_entities'):
                if table in names and 'memory_id' in {r[1] for r in conn.execute('PRAGMA table_info('+q(table)+')')}:
                    conn.execute('DELETE FROM '+q(table)+' WHERE memory_id=?',(local,))
            fields={'text':'','meta':'{}','status':'archived'}
            cols={r[1] for r in conn.execute('PRAGMA table_info(memories)')}
            fields={k:v for k,v in fields.items() if k in cols}
            conn.execute('UPDATE memories SET '+','.join(q(k)+'=?' for k in fields)+' WHERE id=?',(*fields.values(),local))

def atomic_clean(file,data,roots,expected_hash=None):
    file=Path(file).resolve()
    if not any(file==r or file.is_relative_to(r) for r in roots):raise DeleteError('file_outside_managed_scope')
    temporary=file.with_name(file.name+'.redact-'+uuid.uuid4().hex)
    if temporary.resolve().parent!=file.parent:raise DeleteError('unsafe_staging_path')
    try:
        with temporary.open('xb') as out:out.write(data);out.flush();os.fsync(out.fileno())
        if expected_hash and hashlib.sha256(file.read_bytes()).hexdigest()!=expected_hash:
            raise DeleteError('managed_file_changed_retry_offline')
        os.replace(temporary,file) # Clean image only; no old-content backup.
    finally:
        if temporary.exists():temporary.unlink()

def raw_images(target,raw_root,source,pending):
    images={};locations=[];fingerprints={}
    scope=decode(target['source'].get('scope'))
    if isinstance(scope,dict) and all(scope.get(k) for k in ('bot_id','persona','conv_key')):
        session_digest=hashlib.sha256((str(scope['bot_id'])+'|'+str(scope['conv_key'])).encode()).hexdigest()
    else:session_digest=source.get('session_digest')
    if not session_digest:
        raise DeleteError('purge_requires_owned_scope')
    for rid in target['raw_ids']:
        copies=target['raw_records'].get(rid,[])
        if not copies:raise DeleteError('raw_source_missing',details={'raw_id':rid})
        digests=set()
        for copy in copies:
            data=Path(copy['file']).read_bytes()[copy['offset']:copy['offset']+copy['size']]
            digests.add(hashlib.sha256(data).hexdigest())
        if len(digests)>1 and not pending:raise DeleteError('conflicting_raw_id',details={'raw_id':rid})
        for location in copies:
            rec=location['record']
            if hashlib.sha256(str(rec.get('s','')).encode()).hexdigest()!=session_digest:
                raise DeleteError('raw_source_not_in_scope',details={'raw_id':rid})
            identity={k:rec[k] for k in ('id','s','r','message_id') if k in rec}
            overlapping=[k for k,v in identity.items() if literal_hits(str(v),target)]
            if overlapping:
                raise DeleteError('source_identity_contains_requested_content',details={
                    'raw_id':rid,'identity_fields':overlapping,
                    'action':'该内容同时是来源标识；需另行明确会话身份的清理范围，不能静默改ID。'})
            body=rec.get('t','');ranges=[]
            file=Path(location['file']).resolve()
            data=images.setdefault(file,bytearray(file.read_bytes()))
            fingerprints.setdefault(file,hashlib.sha256(file.read_bytes()).hexdigest())
            offset,size=location['offset'],location['size']
            old=bytes(data[offset:offset+size])
            if source.get('raw_clean_sha256',{}).get(rid)==hashlib.sha256(old).hexdigest():
                locations.append((rid,file,offset,old));continue
            for ref in target['refs']:
                if ref.get('raw_id')!=rid:continue
                quote=ref.get('quote')
                if quote:
                    if body.count(quote)==1:ranges.append((body.index(quote),body.index(quote)+len(quote)))
                    elif body.count(quote)>1:raise DeleteError('ambiguous_raw_fragment',details={'raw_id':rid})
                elif 'start' in ref and 'end' in ref and ref['end']<=len(body):
                    ranges.append((ref['start'],ref['end']))
            if not ranges:
                if pending and not literal_hits(rec,target):
                    locations.append((rid,file,offset,old));continue
                raise DeleteError('raw_fragment_not_locatable',details={'raw_id':rid})
            expected={r.get('raw_sha256') for r in target['refs'] if r.get('raw_id')==rid and r.get('raw_sha256')}
            if expected and hashlib.sha256(old).hexdigest() not in expected and not pending:
                raise DeleteError('raw_bytes_changed',details={'raw_id':rid})
            selected=[False]*len(body)
            for start,end in ranges:
                if not 0<=start<end<=len(body):raise DeleteError('invalid_raw_fragment_range')
                selected[start:end]=[True]*(end-start)
            clean=scrub(rec,target)
            clean['t']=''.join(ch for i,ch in enumerate(body) if not selected[i])
            # Fixed-width records keep every unrelated byte offset and hash unchanged.
            blob=json.dumps(clean,ensure_ascii=False,separators=(',',':')).encode('utf8')
            if len(blob)+1>size:raise DeleteError('redaction_would_shift_raw_offsets')
            blob+=b' '*(size-len(blob)-1)+b'\n'
            data[offset:offset+size]=blob;locations.append((rid,file,offset,blob))
    return images,locations,fingerprints

def compact(conn):
    if 'memories_fts' in tables(conn):
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('optimize')")
        conn.commit()
    checkpoint=conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    if checkpoint and checkpoint[0]:raise DeleteError('wal_checkpoint_busy')
    conn.execute('VACUUM')
    checkpoint=conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    if checkpoint and checkpoint[0]:raise DeleteError('wal_checkpoint_busy')

def purge(db,mid,raw_root,bridge_states,*,actor='operator',needle=None,offline_check=assert_offline,fault=None):
    mid=validated_memory_id(mid);actor_id(actor);offline_check()
    db=Path(db).resolve();raw_root=Path(raw_root).resolve()
    roots=[raw_root,*[Path(p).resolve() for p in bridge_states]]
    conn=sqlite3.connect(db.as_uri()+'?mode=rw',uri=True,timeout=0)
    conn.row_factory=sqlite3.Row;extra={}
    try:
        conn.execute('PRAGMA secure_delete=ON');conn.execute('PRAGMA locking_mode=EXCLUSIVE')
        conn.execute('BEGIN EXCLUSIVE');conn.commit()
        target=describe_target(conn,mid,raw_root,needle)
        if not target['body_present'] and not needle:
            raise DeleteError('original_memory_probe_required_after_hard',details={'action':'通过needle-stdin提供你已知的原条目正文；不能仅凭ID宣称清零。'})
        if target['shared']:raise DeleteError('shared_source_requires_explicit_plan',details={'shared_memories':target['shared']})
        if target['shared_entities']:raise DeleteError('shared_entity_requires_explicit_plan',details={'shared_entities':target['shared_entities']})
        if not target['needles']:raise DeleteError('content_probe_unavailable_do_not_claim_zero')
        before=audit(conn,mid,raw_root,bridge_states,target=target)
        if before['errors']:raise DeleteError('audit_incomplete',details={'errors':before['errors']})
        # Lock every SQLite participant before the first content change.
        for root in bridge_states:
            for file in eligible_files(root):
                if file.read_bytes()[:16]!=b'SQLite format 3\x00':continue
                c=sqlite3.connect(file.resolve().as_uri()+'?mode=rw',uri=True,timeout=0);c.row_factory=sqlite3.Row
                extra[str(file.resolve())]=c
                c.execute('PRAGMA secure_delete=ON');c.execute('PRAGMA locking_mode=EXCLUSIVE');c.execute('BEGIN EXCLUSIVE');c.commit()
        source=descriptor(conn,mid,raw_root);source.update(origin='offline_purge')
        last=conn.execute('SELECT mode FROM memory_delete_log WHERE memory_id=? ORDER BY id DESC LIMIT 1',(mid,)).fetchone() if 'memory_delete_log' in tables(conn) else None
        pending=bool(last and last[0]=='purge_pending')
        images,locations,fingerprints=raw_images(target,raw_root,source,pending)
        # Hash the clean records only: resume can recognize them without saving deleted text.
        source['raw_clean_sha256']={rid:hashlib.sha256(blob).hexdigest() for rid,_,_,blob in locations}
        schema(conn);conn.commit()
        # This journal contains addresses only; a crash is resumed forward, never restored from a content backup.
        write_log(conn,mid,'purge_pending',actor,source);conn.commit()
        for key,c in extra.items():
            c.execute('BEGIN IMMEDIATE')
            try:clean_database(c,target);c.commit()
            except BaseException:c.rollback();raise
            compact(c)
        for root in bridge_states:
            for file in eligible_files(root):
                if str(file.resolve()) in extra:continue
                data=file.read_bytes()
                try:text=data.decode('utf-8-sig')
                except UnicodeError:raise DeleteError('unreadable_managed_file')
                clean=clean_file(file,text,target)
                if clean!=text:atomic_clean(file,clean.encode('utf8'),roots,hashlib.sha256(data).hexdigest())
        for file,data in images.items():atomic_clean(file,bytes(data),roots,fingerprints[file])
        if fault:fault('raw_replaced')
        conn.execute('BEGIN IMMEDIATE')
        try:
            clean_database(conn,target,backend=True)
            for rid,file,offset,blob in locations:
                if 'memory_pipeline_raw' in tables(conn):
                    conn.execute('UPDATE memory_pipeline_raw SET offset=?,size=?,sha256=?,processed=2 WHERE raw_id=?',
                                 (offset,len(blob),hashlib.sha256(blob).hexdigest(),rid))
            hard_rows(conn,mid,target['entity_ids'],purge_pending=True)
            conn.commit()
        except BaseException:conn.rollback();raise
        if fault:fault('backend_committed')
        compact(conn)
        after=audit(conn,mid,raw_root,bridge_states,needle=needle,target=target,extra_connections=extra)
        if not after['verified_zero_managed']:
            raise DeleteError('purge_residuals_remain',details={'audit':after})
        source['purged']=True
        write_log(conn,mid,'purge_completed',actor,source);conn.commit()
        compact(conn)
        return {'mode':'purge','memory_id':mid,'verified_zero_managed':True,'undo_supported':False,
            'audit':after,'outside_scope':OUTSIDE,
            'scope_notice':'本范围内已清零，范围外还有：浏览器缓存、DSH/QQ、未纳管备份、非文本附件和系统快照。按原句与来源标识验收，不代表全盘或对方副本已消失。'}
    finally:
        for c in extra.values():c.close()
        conn.close()
