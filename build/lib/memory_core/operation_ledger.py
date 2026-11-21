"""Forward-only operation receipts; queries fail open, mutations stay atomic."""
from __future__ import annotations

from datetime import datetime,timedelta,timezone
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import time
import uuid

from .memory_evidence import agent_conversation,owns_memory,scope_from,scope_key

SHANGHAI=timezone(timedelta(hours=8),'Asia/Shanghai')
AUTO_SOURCE='auto:gate1'

def dump(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def ensure_schema(conn):
    """Schema only, no commit and no historical event generation."""
    conn.execute('''CREATE TABLE IF NOT EXISTS memory_retirement_receipts(
        scope TEXT NOT NULL,request_id TEXT NOT NULL,payload TEXT NOT NULL,
        receipt TEXT NOT NULL,PRIMARY KEY(scope,request_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS memory_retirement_audit(
        id INTEGER PRIMARY KEY,scope TEXT NOT NULL,request_id TEXT NOT NULL,
        operation TEXT NOT NULL,memory_id INTEGER NOT NULL,fingerprint TEXT NOT NULL,
        reason TEXT NOT NULL,operator TEXT NOT NULL,created_at REAL NOT NULL)''')
    cols={r[1]for r in conn.execute('PRAGMA table_info(judgment_ledger)')}
    if cols:
        if'event_json'not in cols:
            conn.execute("ALTER TABLE judgment_ledger ADD COLUMN event_json TEXT NOT NULL DEFAULT '{}'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_gate_judgment_request ON judgment_ledger(scope,expected) WHERE source='auto:gate1'")

def event(kind,subject,reason_code,actor,reversible,scope,at,**extra):
    if not isinstance(subject,dict)or subject.get('type')not in('memory','raw','query')or subject.get('id')in(None,''):
        raise ValueError('ledger_subject_required')
    if actor not in('user','assistant','auto','unknown'):
        raise ValueError('invalid_ledger_actor')
    normalized=scope_from(scope)if scope is not None else None
    value=dict(kind=kind,subject=subject,reason_code=str(reason_code)[:80],actor=actor,
        reversible=bool(reversible),at=float(at),at_text=datetime.fromtimestamp(float(at),SHANGHAI).isoformat(timespec='seconds'),
        scope=normalized,scope_status='bound'if normalized else'unassigned',**extra)
    conversation=agent_conversation(normalized) if normalized else None
    if conversation is not None:value['conversation']=conversation
    if subject['type']in('memory','raw'):value.setdefault(subject['type']+'_id',subject['id'])
    return value

def _fingerprint(row):
    return hashlib.sha256(dump(dict(row)).encode('utf8')).hexdigest()

def memory_scope(conn,row):
    """Read an explicit owner; never guess a scope from a source prefix."""
    try:
        owner=conn.execute('SELECT scope FROM memory_pipeline_owners WHERE memory_id=?',(row['id'],)).fetchone()
    except sqlite3.OperationalError:
        owner=None
    try:
        meta=json.loads(row['meta']or'{}')
        declared=meta.get('pipeline_scope')if isinstance(meta,dict)else None
        if isinstance(declared,str):declared=json.loads(declared)
        scope=scope_from(json.loads(owner[0]))if owner else scope_from(declared)if declared else None
        if owner and declared and scope_from(declared)!=scope:return None
        return scope
    except (ValueError,TypeError,KeyError):
        return None

def begin_change(conn,mid,operation,reason,operator,*,actor='unknown',scope=None,request_id=None,now=None,reason_code=None):
    if not conn.in_transaction:raise RuntimeError('ledger_change_requires_transaction')
    ensure_schema(conn)
    row=conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()
    if row is None:raise ValueError('ledger_memory_missing')
    scope=scope_from(scope)if scope is not None else memory_scope(conn,row)
    key=scope_key(scope)if scope is not None else''
    now=time.time()if now is None else float(now)
    request_id=request_id or uuid.uuid4().hex
    if actor not in('user','assistant','auto','unknown'):raise ValueError('invalid_ledger_actor')
    reason=str(reason or operation)[:300];operator=str(operator or'unknown')[:120]
    audit=conn.execute('''INSERT INTO memory_retirement_audit
        (scope,request_id,operation,memory_id,fingerprint,reason,operator,created_at)VALUES(?,?,?,?,?,?,?,?)''',
        (key,request_id,operation,mid,_fingerprint(row),reason,operator,now))
    return dict(audit_id=audit.lastrowid,request_id=request_id,scope_key=key,scope=scope,mid=mid,
        operation=operation,reason=reason,operator=operator,actor=actor,at=now,reason_code=reason_code or operation)

def finish_change(conn,ticket,*,reversible,details=None):
    if not conn.in_transaction:raise RuntimeError('ledger_change_requires_transaction')
    if not conn.execute('SELECT 1 FROM memory_retirement_audit WHERE id=? AND memory_id=? AND request_id=?',
            (ticket['audit_id'],ticket['mid'],ticket['request_id'])).fetchone():
        raise ValueError('ledger_prior_audit_required')
    operation=ticket['operation']
    kind='archive'if operation in('archive','soft')else'restore'if operation in('restore','restore_soft')else'delete'
    receipt=event(kind,dict(type='memory',id=ticket['mid']),ticket['reason_code'],ticket['actor'],
        reversible,ticket['scope'],ticket['at'],operation=operation,reason=ticket['reason'],operator=ticket['operator'],
        request_id=ticket['request_id'],audit_id=ticket['audit_id'],memory_id=ticket['mid'],ids=[ticket['mid']],
        created_at=ticket['at'],details=details or{})
    payload=dump(dict(operation=operation,memory_id=ticket['mid'],audit_id=ticket['audit_id']))
    prior=conn.execute('SELECT payload,receipt FROM memory_retirement_receipts WHERE scope=? AND request_id=?',
        (ticket['scope_key'],ticket['request_id'])).fetchone()
    if prior:
        if prior['payload']!=payload:raise ValueError('ledger_request_id_conflict')
        return json.loads(prior['receipt'])
    row=conn.execute('SELECT * FROM memories WHERE id=?',(ticket['mid'],)).fetchone()
    if row is not None:
        conn.execute('UPDATE memory_retirement_audit SET fingerprint=? WHERE id=?',(_fingerprint(row),ticket['audit_id']))
    conn.execute('INSERT INTO memory_retirement_receipts VALUES(?,?,?,?)',
        (ticket['scope_key'],ticket['request_id'],payload,dump(receipt)))
    return receipt

class RestoreError(ValueError):
    """Public restore failures contain constant codes, never input or SQL text."""
    def __init__(self,error,code=409):
        super().__init__(error)
        self.error,self.code=error,code


def _restore_code(reason):
    if reason=='restore_unavailable':return 503
    if reason=='memory_not_owned':return 403
    if reason in('invalid_soft_previous_state','restore_history_invalid'):return 409
    if reason=='scope_required' or reason.startswith('invalid_'):return 400
    return 409


def _restore_id(value):
    return type(value) is int and value>0


def _restore_request_id(value):
    return isinstance(value,str) and bool(value.strip()) and len(value)<=200


def _restore_owned_row(memory,scope,mid):
    row=memory.store.conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()
    if row is None or not owns_memory(memory,row,scope) or memory_scope(memory.store.conn,row)!=scope:
        return None
    return row


def _restore_receipt(conn,scope,audit):
    """Match both receipt formats to this exact scoped audit, without returning text."""
    prior=conn.execute('SELECT payload,receipt FROM memory_retirement_receipts WHERE scope=? AND request_id=?',
                       (scope_key(scope),audit['request_id'])).fetchone()
    if prior is None:return None
    try:
        payload,receipt=json.loads(prior['payload']),json.loads(prior['receipt'])
        if not isinstance(payload,dict) or not isinstance(receipt,dict):return None
        if payload.get('operation')!=audit['operation']:return None
        if 'audit_id' in payload:
            if payload['audit_id']!=audit['id'] or payload.get('memory_id')!=audit['memory_id']:return None
        else:
            data=payload.get('data')
            if payload.get('scope')!=scope or not isinstance(data,dict) or data.get('request_id')!=audit['request_id']:return None
            if not any(isinstance(item,dict) and item.get('id')==audit['memory_id'] for item in data.get('items',[])):return None
        if (receipt.get('scope')!=scope or receipt.get('request_id')!=audit['request_id']
                or receipt.get('operation')!=audit['operation']):return None
        items=receipt.get('items',[receipt])
        if not isinstance(items,list):return None
        for item in items:
            if (isinstance(item,dict) and item.get('scope')==scope and item.get('audit_id')==audit['id']
                    and item.get('memory_id')==audit['memory_id'] and item.get('operation')==audit['operation']
                    and item.get('request_id')==audit['request_id']):
                return item
    except (ValueError,TypeError,KeyError):
        pass
    return None


def restore_status(memory,scope,memory_id,*,expected_audit_id=None,expected_request_id=None):
    """Read-only eligibility snapshot; mutations must recheck inside their transaction.

    ``scope`` is a trusted caller's explicit scope, not an authorization grant.
    Denials never disclose audit/request identifiers from another scope.
    """
    def denied(reason):
        return dict(allowed=False,route=None,reason_code=reason,audit_id=None,request_id=None)
    try:scope=scope_from(scope)
    except (ValueError,TypeError):return denied('scope_required')
    if not _restore_id(memory_id):return denied('invalid_memory_id')
    if expected_audit_id is not None and not _restore_id(expected_audit_id):return denied('invalid_source_audit_id')
    if expected_request_id is not None and not _restore_request_id(expected_request_id):return denied('invalid_source_request_id')
    from .memory_retirement import _meta,_daybook
    try:
        with memory.lock,memory.store._lock:
            conn=memory.store.conn
            names={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'memories','memory_retirement_audit','memory_retirement_receipts'}<=names:
                return denied('restore_history_missing')
            row=_restore_owned_row(memory,scope,memory_id)
            if row is None:return denied('memory_not_owned')
            if row['status']!='archived':return denied('memory_not_archived')
            meta=_meta(row)
            if meta is None or _daybook(meta):return denied('daybook_or_invalid_projection_restore_denied')
            # Global latest first: filtering by scope would hide a newer foreign operation.
            audit=conn.execute('SELECT * FROM memory_retirement_audit WHERE memory_id=? ORDER BY id DESC LIMIT 1',
                               (memory_id,)).fetchone()
            if audit is None or audit['scope']!=scope_key(scope):return denied('source_not_current')
            if not _restore_id(audit['id']) or not _restore_request_id(audit['request_id']):return denied('restore_history_invalid')
            if ((expected_audit_id is not None and audit['id']!=expected_audit_id)
                    or (expected_request_id is not None and audit['request_id']!=expected_request_id)):
                return denied('source_not_current')
            if audit['operation'] not in ('soft','archive'):return denied('operation_not_restorable')
            if audit['fingerprint']!=_fingerprint(row):return denied('fingerprint_changed')
            if _restore_receipt(conn,scope,audit) is None:return denied('source_receipt_missing')
            deletion=conn.execute('SELECT * FROM memory_delete_log WHERE memory_id=? ORDER BY id DESC LIMIT 1',
                                  (memory_id,)).fetchone() if 'memory_delete_log' in names else None
            if deletion and deletion['mode'] in ('hard','purge','purge_pending','purge_completed'):
                return denied('operation_not_restorable')
            if audit['operation']=='soft':
                if deletion is None or deletion['mode']!='soft':return denied('soft_log_not_current')
                source=json.loads(deletion['source'])
                if not isinstance(source,dict) or source.get('audit_id')!=audit['id']:
                    return denied('soft_log_not_current')
                previous_at=source.get('previous_archived_at')
                if (source.get('previous_status') not in ('active','archived') or type(previous_at) not in (int,float)
                        or not 0<=previous_at<float('inf')):
                    return denied('invalid_soft_previous_state')
            return dict(allowed=True,route=audit['operation'],reason_code='allowed',audit_id=audit['id'],request_id=audit['request_id'])
    except sqlite3.Error:return denied('restore_unavailable')
    except (ValueError,TypeError,KeyError):return denied('restore_history_invalid')


def _restore_replay(memory,scope,mid,source_audit_id,source_request_id,request_id,*,route=None):
    """Return the original safe result, or reject reuse for a different source/target."""
    conn=memory.store.conn
    prior=conn.execute('SELECT receipt FROM memory_retirement_receipts WHERE scope=? AND request_id=?',
                       (scope_key(scope),request_id)).fetchone()
    if prior is None:return None
    if _restore_owned_row(memory,scope,mid) is None:raise RestoreError('memory_not_owned',403)
    try:
        receipt=json.loads(prior['receipt'])
        items=receipt.get('items',[receipt])
        if not isinstance(items,list) or len(items)!=1:raise ValueError()
        item=items[0]
        actual_route={'restore_soft':'soft','restore':'archive'}.get(item.get('operation'))
        details=item.get('details',{})
        if (not actual_route or route is not None and actual_route!=route or item.get('memory_id')!=mid
                or source_audit_id is not None and details.get('source_audit_id')!=source_audit_id
                or source_request_id is not None and details.get('source_request_id')!=source_request_id
                or item.get('scope')!=scope or item.get('request_id')!=request_id):
            raise ValueError()
        source_audit_id,source_request_id=details.get('source_audit_id'),details.get('source_request_id')
        if not _restore_id(source_audit_id) or not _restore_request_id(source_request_id):raise ValueError()
        audit=conn.execute('SELECT * FROM memory_retirement_audit WHERE id=? AND scope=? AND memory_id=? AND request_id=?',
                           (item.get('audit_id'),scope_key(scope),mid,request_id)).fetchone()
        if audit is None or _restore_receipt(conn,scope,audit) is None:raise ValueError()
        restored_status=details.get('restored_status')
        if restored_status not in ('active','archived'):raise ValueError()
        return dict(ok=True,memory_id=mid,route=actual_route,audit_id=audit['id'],request_id=request_id,
                    source_audit_id=source_audit_id,source_request_id=source_request_id,restored_status=restored_status)
    except (ValueError,TypeError,KeyError,AttributeError):
        raise RestoreError('request_id_conflict') from None


def restore_from_panel(memory,data):
    """Restore one exact source through existing writers under authenticated ACCESS.

    Input has four operation identifiers and optional declared scope fields;
    authenticated ACCESS is always required and any declared scope must match.
    Output is receipt metadata only; a replay describes that original operation.
    """
    from .scope_auth import current,ScopeError
    from .memory_delete import restore_soft,DeleteError
    from .memory_retirement import MemoryRetirement,RetirementError
    try:scope=current(data).scope
    except ScopeError as error:
        raise RestoreError('scope_required' if error.code==400 else 'scope_forbidden',error.code) from None
    required={'memory_id','source_audit_id','source_request_id','request_id'}
    if not isinstance(data,dict) or not required<=set(data) or set(data)-required-{'bot_id','persona','conv_key'}:
        raise RestoreError('invalid_restore_request',400)
    mid,source_id=data['memory_id'],data['source_audit_id']
    source_request,request_id=data['source_request_id'],data['request_id']
    if not _restore_id(mid):raise RestoreError('invalid_memory_id',400)
    if not _restore_id(source_id):raise RestoreError('invalid_source_audit_id',400)
    if not _restore_request_id(source_request):raise RestoreError('invalid_source_request_id',400)
    if not _restore_request_id(request_id):raise RestoreError('invalid_request_id',400)
    try:
        with memory.lock,memory.store._lock:
            if memory.store.conn.in_transaction:raise RestoreError('restore_transaction_conflict')
            replay=_restore_replay(memory,scope,mid,source_id,source_request,request_id)
            if replay is not None:return replay
            status=restore_status(memory,scope,mid,expected_audit_id=source_id,expected_request_id=source_request)
            if not status['allowed']:
                raise RestoreError(status['reason_code'],_restore_code(status['reason_code']))
            if status['route']=='soft':
                restore_soft(memory.store,mid,scope=scope,expected_audit_id=source_id,
                             expected_request_id=source_request,request_id=request_id,actor='ledger_panel',actor_kind='user')
            else:
                MemoryRetirement(memory).restore({**scope,'request_id':request_id,'items':[{'id':mid}],
                    'reason':'panel_restore','operator':'ledger_panel','actor_kind':'user',
                    'expected_audit_id':source_id,'expected_request_id':source_request})
            result=_restore_replay(memory,scope,mid,source_id,source_request,request_id,route=status['route'])
            if result is None:raise RestoreError('restore_receipt_missing',503)
            return result
    except sqlite3.Error:
        raise RestoreError('restore_unavailable',503) from None
    except (DeleteError,RetirementError) as error:
        raise RestoreError(error.error,error.code) from None

def _gap(result):
    state=result['state'];missing=result.get('missing')or''
    if state=='not_checked':return ['not_checked'],'未查询'
    if state=='absent':
        return ['no_eligible_evidence'],'未找到合格证据'if not result.get('source_clues')else'只有不合格来源线索，未找到合格证据'
    if state=='found':return [],'无缺口'
    codes=[];labels=[]
    for condition,code,label in(
        ('否定方向未能确认'in missing,'polarity_unconfirmed','否定方向未确认'),
        ('时间对不上'in missing or'日期'in missing,'date_unconfirmed','时间未确认'),
        ('不确定是哪'in missing,'reference_ambiguous','指代未确认'),
        ('当前上下文'in missing,'recent_only','仅有当前上下文'),
        ('原文没提到查询里的'in missing,'content_gap','查询内容未全部覆盖')):
        if condition:codes.append(code);labels.append(label)
    if'content_gap'in codes:
        prefix=missing.partition('；')[0]
        terms=re.findall(r'「([^」]{1,40})」',prefix)[:10]
        if terms:labels.append('缺少：'+'、'.join(terms))
    return codes or['coverage_incomplete'],'；'.join(labels)or'证据覆盖不全'

def _failure(scope,query_id,code,error=None):
    detail=dict(status='failed',query_id=query_id,reason_code=code)
    try:
        print('[ledger_write_failed] '+dump(dict(scope=scope,query_id=query_id,reason_code=code,
            error_type=type(error).__name__ if error else None)),flush=True)
    except (OSError,ValueError):pass
    return detail

def record_gate(memory,data,result,at):
    """One zero-wait, separate connection; failures never replace the gate state."""
    scope=scope_from(data);query_id=data.get('query_id')
    if query_id is None:query_id=uuid.uuid4().hex
    if not isinstance(query_id,str)or not query_id.strip()or len(query_id)>128:
        return _failure(scope,None,'invalid_query_id')
    store=getattr(memory,'store',None);path=getattr(store,'path',None)
    if not isinstance(path,(str,Path)):
        return dict(status='skipped',reason_code='no_writable_store',query_id=query_id)
    # An explicit query-only snapshot must never become a second writable connection.
    if not store._lock.acquire(blocking=False):return _failure(scope,query_id,'store_busy')
    try:
        conn=getattr(store.conn,'raw',store.conn)
        if conn.execute('PRAGMA query_only').fetchone()[0]:
            return dict(status='skipped',reason_code='read_only_snapshot',query_id=query_id)
    finally:store._lock.release()
    evidence=result.get('evidence')or{};codes,summary=_gap(result)
    query=str(data.get('text')or'')[:512]
    payload=event('judge',dict(type='query',id=query_id),result['state'],'auto',False,scope,at,
        query_id=query_id,query=query,state=result['state'],evidence_raw_id=evidence.get('raw_id'),
        evidence_memory_id=evidence.get('memory_id'),memory_id=evidence.get('memory_id'),gap_codes=codes,gap_summary=summary)
    journal=None
    try:
        journal=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw',uri=True,timeout=0)
        journal.row_factory=sqlite3.Row;journal.execute('BEGIN IMMEDIATE')
        # Store initialization installs the column/index; no DDL in the query path.
        old=journal.execute('SELECT id,event_json FROM judgment_ledger WHERE scope=? AND source=? AND expected=?',
            (scope_key(scope),AUTO_SOURCE,query_id)).fetchone()
        if old:
            old_payload=json.loads(old['event_json'])
            if any(old_payload.get(key)!=payload.get(key)for key in(
                    'query','state','evidence_raw_id','evidence_memory_id','gap_codes')):
                raise ValueError('query_id_conflict')
            journal.rollback();return dict(status='recorded',query_id=query_id,ledger_id=old['id'],duplicate=True)
        row=journal.execute('''INSERT INTO judgment_ledger
            (kind,claim,reason,expected,outcome,verdict,scope,source,created_at,updated_at,event_json)
            VALUES('judge',?,?,?,'','recorded',?,?,?,?,?)''',
            ('门一 '+result['state']+'：'+query[:300],summary,query_id,scope_key(scope),AUTO_SOURCE,float(at),float(at),dump(payload)))
        journal.commit();return dict(status='recorded',query_id=query_id,ledger_id=row.lastrowid,duplicate=False)
    except (sqlite3.Error,OSError,ValueError)as error:
        if journal is not None:journal.rollback()
        return _failure(scope,query_id,'ledger_write_failed',error)
    finally:
        if journal is not None:journal.close()

def recent(memory,data):
    """Scoped read projection only. Does not initialize schemas or fabricate old events."""
    from .scope_auth import current
    scope=current(data).scope;key=scope_key(scope)
    limit=int(data.get('limit',20))
    if not 1<=limit<=20:raise ValueError('ledger_limit_must_be_1_to_20')
    out=dict(scope=scope,limit=limit,judgments=[],retirements=[],extractions=[])
    with memory.lock,memory.store._lock:
        conn=memory.store.conn
        names={r[0]for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if'judgment_ledger'in names and'event_json'in{r[1]for r in conn.execute('PRAGMA table_info(judgment_ledger)')}:
            for row in conn.execute('SELECT id,event_json FROM judgment_ledger WHERE scope=? AND source=? ORDER BY id DESC LIMIT ?',
                    (key,AUTO_SOURCE,limit)):
                value=json.loads(row['event_json'])
                if value.get('scope')==scope:out['judgments'].append(dict(value,ledger_id=row['id']))
        if'memory_retirement_receipts'in names:
            for row in conn.execute('SELECT receipt FROM memory_retirement_receipts WHERE scope=? ORDER BY rowid DESC LIMIT ?',(key,limit)):
                value=json.loads(row['receipt'])
                for item in value.get('items',[value]):
                    if isinstance(item,dict)and item.get('scope')==scope and item.get('kind'):
                        out['retirements'].append(item)
            out['retirements']=sorted(out['retirements'],key=lambda e:e['at'],reverse=True)[:limit]
        if'memory_pipeline_audit'in names:
            for row in conn.execute(
                    "SELECT id,detail FROM memory_pipeline_audit WHERE scope=? AND operation='raw_soft_delete' "
                    "ORDER BY id DESC LIMIT ?", (key, limit)):
                value = json.loads(row['detail'])
                if value.get('scope') == scope:
                    out['retirements'].append(dict(value, audit_id=row['id']))
            out['retirements'] = sorted(out['retirements'], key=lambda e:e['at'], reverse=True)[:limit]
            for row in conn.execute("SELECT id,detail FROM memory_pipeline_audit WHERE scope=? AND json_valid(detail) "
                    "AND json_extract(detail,'$.kind') IN ('extract','drop','merge','downgrade') ORDER BY id DESC LIMIT ?",(key,limit)):
                value=json.loads(row['detail'])
                if value.get('scope')==scope and value.get('kind')in('extract','drop','merge','downgrade'):
                    out['extractions'].append(dict(value,audit_id=row['id']))
        if'memory_delete_log'in names:
            from .delete_audit import scope_digest
            for row in conn.execute("SELECT memory_id,created_at,actor FROM memory_delete_log WHERE mode='purge_completed' "
                    "AND json_valid(source) AND json_extract(source,'$.scope_digest')=? ORDER BY id DESC LIMIT ?",(scope_digest(scope),limit)):
                out['retirements'].append(event('delete',dict(type='memory',id=row['memory_id']),'purge_completed',
                    'unknown',False,scope,row['created_at'],operation='purge',operator=row['actor'],
                    receipt_source='memory_delete_log',reason='离线彻底清理完成，仅限既有受管范围'))
            out['retirements']=sorted(out['retirements'],key=lambda e:e['at'],reverse=True)[:limit]
    return out
