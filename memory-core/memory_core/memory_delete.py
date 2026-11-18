"""Explicit soft/hard deletion. Purge is an offline, cross-store maintenance operation."""
from __future__ import annotations
import json
import hashlib
import re
import time
from .delete_audit import describe_target,tables,decode,q,REDACTED
from .delete_audit import scope_digest

OUTSIDE=['浏览器缓存','DSH/QQ及模型服务端会话','未纳管备份和其它目录','截图/音视频等非文本附件','磁盘取证/SSD/文件系统快照']
class DeleteError(ValueError):
    def __init__(self,error,*,code=409,details=None):
        super().__init__(error);self.code=code;self.error=error;self.details=details or {}

def schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS memory_delete_log(
        id INTEGER PRIMARY KEY,memory_id INTEGER NOT NULL,mode TEXT NOT NULL,
        created_at REAL NOT NULL,actor TEXT NOT NULL,source TEXT NOT NULL)''')

def actor_id(actor):
    if not isinstance(actor,str) or not re.fullmatch(r'[A-Za-z0-9_.:@-]{1,80}',actor):
        raise DeleteError('actor_must_be_short_operator_id',code=400)
    return actor

def validated_memory_id(value):
    if type(value) is int and value>0:return value
    if isinstance(value,str) and re.fullmatch(r'[1-9][0-9]*',value):return int(value)
    raise DeleteError('memory_id_must_be_positive_integer',code=400)

def descriptor(conn,mid,raw_root):
    target=describe_target(conn,mid,raw_root)
    result=dict(target['source'])
    previous=conn.execute('SELECT source FROM memory_delete_log WHERE memory_id=? ORDER BY id DESC LIMIT 1',(mid,)).fetchone() if 'memory_delete_log' in tables(conn) else None
    oldrefs=(decode(previous[0]).get('refs',[]) if previous else [])
    result['refs']=[]
    for ref in target['refs']:
        rid=ref.get('raw_id')
        if not rid:continue
        item={'raw_id':rid}
        original=target['raw_records'].get(rid,[])
        quote=ref.get('quote')
        if original and isinstance(quote,str) and quote:
            body=original[0]['record'].get('t','')
            if body.count(quote)==1:item.update(start=body.index(quote),end=body.index(quote)+len(quote))
        elif 'start' in ref and 'end' in ref:item.update(start=ref['start'],end=ref['end'])
        if 'start' not in item:
            old=next((r for r in oldrefs if r.get('raw_id')==rid and 'start' in r),None)
            if old:item.update(start=old['start'],end=old['end'])
        result['refs'].append(item)
    result['link_ids']=sorted(target['link_ids'])
    result['entity_ids']=sorted(target['entity_ids'])
    result['message_ids']=sorted(target['message_ids'])
    result['citation_ids']=sorted(target['citation_ids'])
    result.pop('quote',None)
    if 'daybook_projections' in tables(conn):
        result['daybook']=[dict(r) for r in conn.execute('SELECT * FROM daybook_projections WHERE memory_id=?',(mid,))]
    scope=decode(result.pop('scope',None))
    if isinstance(scope,dict) and all(k in scope for k in ('bot_id','persona','conv_key')):
        result['scope_digest']=scope_digest(scope)
        result['session_digest']=hashlib.sha256((str(scope['bot_id'])+'|'+str(scope['conv_key'])).encode()).hexdigest()
    return result

def write_log(conn,mid,mode,actor,source):
    return conn.execute('INSERT INTO memory_delete_log(memory_id,mode,created_at,actor,source) VALUES(?,?,?,?,?)',
                        (mid,mode,time.time(),actor_id(actor),json.dumps(source,ensure_ascii=False,separators=(',',':')))).lastrowid

def hard_rows(conn,mid,entity_ids=(),*,audit_id=None,purge_pending=False):
    if not conn.in_transaction:raise DeleteError('hard_delete_requires_transaction')
    names=tables(conn);links=[]
    if purge_pending and audit_id is None:
        pending=conn.execute('SELECT mode FROM memory_delete_log WHERE memory_id=? ORDER BY id DESC LIMIT 1',(mid,)).fetchone() if 'memory_delete_log' in names else None
        if not pending or pending[0]!='purge_pending':raise DeleteError('latest_purge_pending_required')
    elif type(audit_id) is int and audit_id>0 and not purge_pending:
        audit=conn.execute('''SELECT * FROM memory_retirement_audit a WHERE id=? AND memory_id=? AND operation='hard'
            AND id=(SELECT MAX(id) FROM memory_retirement_audit WHERE memory_id=?)
            AND NOT EXISTS(SELECT 1 FROM memory_retirement_receipts r WHERE r.scope=a.scope AND r.request_id=a.request_id)''',
            (audit_id,mid,mid)).fetchone() if {'memory_retirement_audit','memory_retirement_receipts'}<=names else None
        row=conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()
        fingerprint=hashlib.sha256(json.dumps(dict(row),sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf8')).hexdigest() if row else None
        if not audit or audit['fingerprint']!=fingerprint:raise DeleteError('fresh_hard_delete_audit_required')
    else:raise DeleteError('hard_delete_audit_required')
    entities=set(entity_ids)
    if 'memory_entities' in names:entities.update(r[0] for r in conn.execute('SELECT entity_id FROM memory_entities WHERE memory_id=?',(mid,)))
    if 'memory_links' in names:
        cols={r[1] for r in conn.execute('PRAGMA table_info(memory_links)')}
        links=[dict(r) for r in conn.execute(
            "SELECT * FROM memory_links WHERE (a_kind='memory' AND a_id=?) OR (b_kind='memory' AND b_id=?)",(mid,mid))]
        ids={r['id'] for r in links}
        incoming=set()
        if 'superseded_by' in cols and ids:
            frontier=set(ids)
            while frontier:
                found={r[0] for r in conn.execute('SELECT id FROM memory_links WHERE superseded_by IN ('+','.join('?'*len(frontier))+')',sorted(frontier))}
                frontier=found-incoming-ids;incoming.update(found)
        preserve=incoming | {r['id'] for r in links if r.get('stance_tag') in ('superseded','transition') or r.get('superseded_by') is not None}
        for row in links:
            if 'superseded_by' in cols and conn.execute('SELECT 1 FROM memory_links WHERE superseded_by=?',(row['id'],)).fetchone():preserve.add(row['id'])
        for lid in ids|incoming:
            if lid in preserve:
                updates={'why':REDACTED,'relation':'已删除#'+str(lid),'reason':REDACTED,'stance_tag':'orphaned','weight':0,'t_expired':time.time()}
                updates={k:v for k,v in updates.items() if k in cols}
                conn.execute('UPDATE memory_links SET '+','.join(q(k)+'=?' for k in updates)+' WHERE id=?',(*updates.values(),lid))
                if 'memory_pipeline_link_meta' in names:
                    fields={r[1] for r in conn.execute('PRAGMA table_info(memory_pipeline_link_meta)')}
                    values={'state':'orphaned','source_refs':'[]','reason':REDACTED,'direction':'{}'}
                    values={k:v for k,v in values.items() if k in fields}
                    conn.execute('UPDATE memory_pipeline_link_meta SET '+','.join(q(k)+'=?' for k in values)+' WHERE link_id=?',(*values.values(),lid))
            else:
                if 'memory_pipeline_link_meta' in names:conn.execute('DELETE FROM memory_pipeline_link_meta WHERE link_id=?',(lid,))
                conn.execute('DELETE FROM memory_links WHERE id=?',(lid,))
    for table in ['memories_fts','vectors','memory_entities','memory_pipeline_items','memory_pipeline_owners',
                  'memory_pipeline_reviews','memory_gate_costs','memory_fact_versions','daybook_projections','relations']:
        if table in names and 'memory_id' in {r[1] for r in conn.execute('PRAGMA table_info('+q(table)+')')}:
            conn.execute('DELETE FROM '+q(table)+' WHERE memory_id=?',(mid,))
    if 'conflicts' in names:conn.execute('DELETE FROM conflicts WHERE a_id=? OR b_id=?',(mid,mid))
    conn.execute('DELETE FROM memories WHERE id=?',(mid,))
    # Only truly unreferenced entity nodes are derived leftovers.
    if {'entities','memory_entities','relations'}<=names:
        for eid in entities:
            conn.execute('''DELETE FROM entities WHERE id=? AND id NOT IN (SELECT entity_id FROM memory_entities)
                AND id NOT IN (SELECT src_id FROM relations) AND id NOT IN (SELECT dst_id FROM relations)''',(eid,))

def delete(store,memory_id,mode='soft',*,actor='legacy',origin='store',automatic=False,reason=None,actor_kind=None):
    mid=validated_memory_id(memory_id)
    if mid<1 or mode not in ('soft','hard','purge'):raise DeleteError('invalid_delete_mode_or_id',code=400)
    actor_id(actor)
    if reason is not None and (not isinstance(reason,str)or not reason.strip()or len(reason)>300):
        raise DeleteError('invalid_delete_reason',code=400)
    if actor_kind is not None and actor_kind not in('user','assistant','auto','unknown'):
        raise DeleteError('invalid_actor_kind',code=400)
    if mode=='purge':
        raise DeleteError('purge_requires_offline_maintenance',details={'outside_scope':OUTSIDE,
            'scope_notice':'尚未清理；purge须停服后离线执行，范围外还有：浏览器缓存、DSH/QQ、未纳管备份。'})
    from .operation_ledger import begin_change,finish_change
    conn=getattr(store.conn,'raw',store.conn)
    with store._lock:
        if conn.in_transaction:raise RuntimeError('delete requires its own short transaction')
        conn.execute('BEGIN IMMEDIATE')
        try:
            schema(conn)
            row=conn.execute('SELECT status,archived_at FROM memories WHERE id=?',(mid,)).fetchone()
            if not row:conn.rollback();return False
            if mode=='hard' and automatic:
                from .memory_temporal import retained_memory
                if retained_memory(conn,mid):conn.rollback();return False
            latest=conn.execute('SELECT mode FROM memory_delete_log WHERE memory_id=? ORDER BY id DESC LIMIT 1',(mid,)).fetchone()
            if latest and latest[0]=='purge_pending':raise DeleteError('unfinished_purge_resume_offline')
            if mode=='soft' and row['status']=='archived' and latest and latest[0]=='soft':
                conn.rollback();return True
            source=descriptor(conn,mid,store.path.parent/'raw')
            source.update(origin=origin,previous_status=row['status'],previous_archived_at=row['archived_at'])
            default_reason={'merge':'合并后清理被并入的条目','conflict_resolution':'冲突处理后清理未保留条目',
                            'archive_cleanup':'归档保留期届满，自动硬清理'}.get(origin,'调用方请求'+('软删除记忆' if mode=='soft' else '硬删除记忆'))
            ticket=begin_change(conn,mid,mode,default_reason if reason is None else reason,actor,
                                actor=('auto' if automatic else 'unknown') if actor_kind is None else actor_kind,
                                reason_code=origin)
            source['audit_id']=ticket['audit_id']
            log_id=write_log(conn,mid,mode,actor,source)
            if mode=='soft':conn.execute("UPDATE memories SET status='archived',archived_at=? WHERE id=?",(time.time(),mid))
            else:hard_rows(conn,mid,audit_id=ticket['audit_id'])
            finish_change(conn,ticket,reversible=mode=='soft',details={'origin':origin,'delete_log_id':log_id})
            conn.commit();return True
        except BaseException:conn.rollback();raise

def restore_soft(store,memory_id,*,actor='legacy',reason=None,actor_kind='unknown',scope=None,
                 expected_audit_id=None,expected_request_id=None,request_id=None):
    from types import SimpleNamespace
    from .operation_ledger import (begin_change,finish_change,restore_status,_restore_replay,
                                   _restore_request_id,_restore_code,RestoreError)
    scoped=scope is not None
    if not scoped and any(value is not None for value in (expected_audit_id,expected_request_id,request_id)):
        raise RestoreError('scope_required',400)
    if scoped:
        from .memory_evidence import scope_from
        try:scope=scope_from(scope)
        except (ValueError,TypeError):raise RestoreError('scope_required',400) from None
        if request_id is not None and not _restore_request_id(request_id):raise RestoreError('invalid_request_id',400)
    conn=getattr(store.conn,'raw',store.conn);mid=validated_memory_id(memory_id)
    memory=SimpleNamespace(store=store,lock=store._lock)
    with store._lock:
        if conn.in_transaction:
            if scoped:raise RestoreError('restore_transaction_conflict')
            raise RuntimeError('restore requires its own short transaction')
        conn.execute('BEGIN IMMEDIATE')
        try:
            status=None
            if scoped:
                if request_id is not None and _restore_replay(memory,scope,mid,expected_audit_id,expected_request_id,
                                                             request_id,route='soft') is not None:
                    conn.rollback();return True
                status=restore_status(memory,scope,mid,expected_audit_id=expected_audit_id,expected_request_id=expected_request_id)
                if not status['allowed'] or status['route']!='soft':
                    raise RestoreError(status['reason_code'] if not status['allowed'] else 'not_soft_source',
                                       _restore_code(status['reason_code']))
            else:schema(conn)
            row=conn.execute('SELECT * FROM memory_delete_log WHERE memory_id=? ORDER BY id DESC LIMIT 1',(mid,)).fetchone()
            if not row or row['mode']!='soft' or not conn.execute('SELECT 1 FROM memories WHERE id=?',(mid,)).fetchone():
                raise DeleteError('only_soft_can_be_restored')
            source=decode(row['source'])
            ticket=begin_change(conn,mid,'restore_soft',reason or '恢复软删除前的记忆状态',actor,
                                actor=actor_kind,reason_code='restore_soft',scope=scope,request_id=request_id)
            pointers=({'source_audit_id':status['audit_id'],'source_request_id':status['request_id']} if scoped else {})
            source=({key:source[key] for key in ('previous_status','previous_archived_at')} if scoped else source)
            source=dict(source,audit_id=ticket['audit_id'],**pointers)
            log_id=write_log(conn,mid,'restore_soft',actor,source)
            conn.execute('UPDATE memories SET status=?,archived_at=? WHERE id=?',
                         (source['previous_status'],source['previous_archived_at'],mid))
            finish_change(conn,ticket,reversible=True,details={'delete_log_id':log_id,'restored_status':source['previous_status'],**pointers})
            conn.commit();return True
        except BaseException as error:
            conn.rollback()
            if scoped:
                import sqlite3
                if isinstance(error,sqlite3.Error):raise RestoreError('restore_unavailable',503) from None
            raise
