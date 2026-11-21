"""Scoped, read-only ledger projections. No raw transcript bodies or schema writes."""
from __future__ import annotations

import base64
from collections import Counter
from datetime import datetime,timedelta,timezone
import hashlib
import json
import math
import sqlite3
import time

from .memory_evidence import scope_key
from .scope_auth import current

KINDS={'judge','drop','extract','delete','archive','restore','merge','downgrade'}
SHANGHAI=timezone(timedelta(hours=8),'Asia/Shanghai')
MAX_ROWS=10000
REASONS={
    'not_selected_by_extractor':'提炼器未选择，具体原因未记录；原话仍保留',
    'drop_low_information':'完整原话命中既有低信息规则，未进入条目层',
    'source_backed_extraction':'已有原话提炼为条目',
    'source_support_limited':'来源资格受限，只能保留为线索',
    'same_correction_sources_grouped':'同次更正的多条原话组合成一个端点',
}

class PanelError(ValueError):
    def __init__(self,error,code=400):
        self.error,self.code=error,code
        super().__init__(error)

def _text(value,limit=300):
    return str(value or'')[:limit]

def _json(value):
    try:
        parsed=json.loads(value)if isinstance(value,str)else value
        return parsed if isinstance(parsed,dict)else{}
    except (ValueError,TypeError):return{}

def _number(value,name,default=None):
    if value in(None,''):return default
    try:
        if isinstance(value,bool):raise ValueError()
        number=float(value)
        if not math.isfinite(number)or number<0:raise ValueError()
        return number
    except (ValueError,TypeError):raise PanelError('invalid_'+name)from None

def _ids(value):
    return list(dict.fromkeys(v for v in value if isinstance(v,str)and 0<len(v)<=256))[:32]if isinstance(value,list)else[]

def _options(data,scope,now):
    try:
        limit=int(data.get('limit',50));days=int(data.get('days',7))
        if isinstance(data.get('limit'),bool)or isinstance(data.get('days'),bool)or not 1<=limit<=100 or not 0<=days<=3660:raise ValueError()
    except (ValueError,TypeError):raise PanelError('invalid_page_size_or_days')from None
    kinds={part.strip()for part in str(data.get('kind')or'').split(',')if part.strip()}
    if kinds-KINDS:raise PanelError('invalid_ledger_kind')
    after=_number(data.get('after'),'after');before=_number(data.get('before'),'before')
    binding=hashlib.sha256(json.dumps(dict(scope=scope,kind=sorted(kinds),days=days,after=after,before=before),
        ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    cursor=data.get('cursor');last=None;as_of=now
    if cursor:
        try:
            if not isinstance(cursor,str)or len(cursor)>1500:raise ValueError()
            decoded=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
            if decoded['v']!=1 or decoded['binding']!=binding:raise ValueError()
            as_of=float(decoded['as_of']);last=decoded['last']
            if not math.isfinite(as_of)or not 0<as_of<=now+5:raise ValueError()
            if not isinstance(last,list)or len(last)!=2 or not isinstance(last[1],str):raise ValueError()
            last=(float(last[0]),last[1])
            if not math.isfinite(last[0]):raise ValueError()
        except (ValueError,TypeError,KeyError,UnicodeError):raise PanelError('invalid_or_mismatched_cursor')from None
    upper=before if before is not None else as_of
    lower=after if after is not None else max(0,as_of-days*86400)if days else 0
    if lower>=upper:raise PanelError('invalid_time_range')
    return dict(limit=limit,kind=kinds,start=lower,end=upper,as_of=as_of,last=last,binding=binding)

def _base(scope,event_id,source,at,kind,**values):
    stamp=_number(at,'event_time')
    if stamp is None:raise ValueError('missing_event_time')
    item=dict(event_id=event_id,source=source,at=stamp,
        at_text=datetime.fromtimestamp(stamp,SHANGHAI).isoformat(timespec='seconds'),kind=kind,
        state=None,operation=None,query='',gap_summary='',evidence_raw_id=None,memory_id=None,
        raw_ids=[],reversible=False,actor='unknown',reason_code='',reason='',scope=scope,
        environment='unknown',review_status='not_recorded',support_target='not_recorded')
    item.update(values)
    if item['actor']not in('user','assistant','auto','unknown'):item['actor']='unknown'
    return item

def _load(memory,scope,opts):
    """ponytail: bounded per-scope projection; add indexed views if a window exceeds 10k rows."""
    events=[];issues=Counter();sources={};key=scope_key(scope)
    conn=memory.store.conn
    names={r[0]for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    def rows(table,sql,args):
        if table not in names:
            sources[table]='missing';return[]
        found=conn.execute(sql,args).fetchmany(MAX_ROWS+1)
        if len(found)>MAX_ROWS:raise PanelError('time_window_too_wide',413)
        sources[table]='read';return[dict(r)for r in found]
    def add(value):
        if opts['start']<=value['at']<opts['end']:events.append(value)
    def checked(value):
        if value.get('scope')!=scope:
            issues['missing_or_conflicting_scope']+=1;return False
        return True
    judgments=rows('judgment_ledger',"SELECT id,event_json FROM judgment_ledger WHERE scope=? "
        "AND created_at>=? AND created_at<? AND source='auto:gate1'",(key,opts['start'],opts['end']))
    for row in judgments:
        value=_json(row['event_json'])
        if not checked(value):continue
        if value.get('state')not in('found','suspected','absent','not_checked'):
            issues['unmapped_judgment']+=1;continue
        add(_base(scope,'judge:'+str(row['id']),'judgment_ledger',value.get('at'),'judge',
            query=_text(value.get('query'),512),state=value['state'],gap_summary=_text(value.get('gap_summary')),
            reason_code=value['state'],evidence_raw_id=value.get('evidence_raw_id'),
            memory_id=value.get('evidence_memory_id'),query_id=value.get('query_id'),
            actor=value.get('actor','auto'),gap_codes=value.get('gap_codes',[])))
    pipeline=rows('memory_pipeline_audit','SELECT * FROM memory_pipeline_audit WHERE scope=? AND created_at>=? AND created_at<?',
        (key,opts['start'],opts['end']))
    for row in pipeline:
        value=_json(row['detail'])
        kind=value.get('kind')
        if row['operation']=='link_skipped':
            # Existing optional-link refusal is not a discarded raw message.
            add(_base(scope,'pipeline:'+str(row['id']),'memory_pipeline_audit',row['created_at'],'drop',
                operation='link_skipped',reason_code=_text(value.get('reason_code')or'link_rejected',80),
                reason=_text(value.get('reason_text')or'连线未获许可，条目本体仍按原规则处理'),
                memory_id=value.get('a')if type(value.get('a'))is int else None,
                related_memory_id=value.get('b')if type(value.get('b'))is int else None,
                discarded_object='link_proposal',job_id=value.get('job_id'),actor='auto'))
            continue
        if kind not in('drop','extract','merge','downgrade'):
            issues['unmapped_pipeline_events']+=1;continue
        if not checked(value):continue
        add(_base(scope,'pipeline:'+str(row['id']),'memory_pipeline_audit',row['created_at'],kind,
            operation=row['operation'],reason_code=_text(value.get('reason_code'),80),
            reason=_text(value.get('reason_text')or REASONS.get(value.get('reason_code'))),memory_id=value.get('memory_id'),
            raw_ids=_ids(value.get('raw_ids')),actor=value.get('actor','auto'),job_id=value.get('job_id')))
    jobs=rows('memory_pipeline_jobs',"SELECT id,raw_ids,status,attempts,max_attempts,next_at,error,updated_at "
        "FROM memory_pipeline_jobs WHERE scope=? AND status='failed' AND updated_at>=? AND updated_at<?",
        (key,opts['start'],opts['end']))
    for row in jobs:
        try:raw_ids=_ids(json.loads(row['raw_ids']))
        except (ValueError,TypeError):raw_ids=[]
        exhausted=row['attempts']>=row['max_attempts']
        error_type=str(row['error']).partition(':')[0]
        if error_type not in('ValueError','TimeoutError','RuntimeError','JSONDecodeError'):error_type='unclassified_error'
        add(_base(scope,'job:'+row['id'],'memory_pipeline_jobs',row['updated_at'],'extract',state='failed',
            operation='failed',reason_code='extraction_failed',reason='提炼失败；'+('重试次数已用尽'if exhausted else'等待重试'),
            raw_ids=raw_ids,job_id=row['id'],attempts=row['attempts'],max_attempts=row['max_attempts'],
            error_type=error_type,record_nature='current_job_status_not_complete_failure_history',actor='auto'))
    audits=rows('memory_retirement_audit','SELECT * FROM memory_retirement_audit WHERE scope=? AND created_at>=? AND created_at<?',
        (key,opts['start'],opts['end']))
    receipts=rows('memory_retirement_receipts',"SELECT request_id,receipt FROM memory_retirement_receipts WHERE scope=? "
        "AND json_valid(receipt) AND COALESCE(json_extract(receipt,'$.at'),json_extract(receipt,'$.created_at'),0)>=? "
        "AND COALESCE(json_extract(receipt,'$.at'),json_extract(receipt,'$.created_at'),0)<?",(key,opts['start'],opts['end']))
    receipt_map={}
    for row in receipts:
        value=_json(row['receipt'])
        if value.get('scope')not in(None,scope):
            issues['receipt_scope_mismatch']+=1;continue
        items=value.get('items',[value])
        if not isinstance(items,list):issues['malformed_receipt']+=1;continue
        for part in items:
            if not isinstance(part,dict):issues['malformed_receipt']+=1;continue
            if part.get('scope')not in(None,scope):
                issues['receipt_scope_mismatch']+=1;continue
            mids=[part['memory_id']]if part.get('memory_id')is not None else part.get('ids',[])
            if not isinstance(mids,list):continue
            for mid in mids:
                if type(mid)is int:receipt_map[(row['request_id'],mid)]=(value,part)
    used=set()
    for audit in audits:
        operation=audit['operation']
        if operation not in('soft','hard','purge','archive','restore','restore_soft'):
            issues['unmapped_retirement_events']+=1;continue
        kind='delete'if operation in('soft','hard','purge')else'archive'if operation=='archive'else'restore'
        match=receipt_map.get((audit['request_id'],audit['memory_id']))
        whole,part=match if match else({},{})
        complete=bool(match and(part.get('operation')or whole.get('operation'))==operation
            and part.get('audit_id')in(None,audit['id'])and part.get('request_id',audit['request_id'])==audit['request_id'])
        if complete:used.add((audit['request_id'],audit['memory_id']))
        else:issues['audit_without_completion']+=1
        capability=dict(allowed=False,reason_code='not_a_current_archive',route=None)
        if complete and operation in('soft','archive'):
            from .operation_ledger import restore_status
            capability=restore_status(memory,scope,audit['memory_id'],
                expected_audit_id=audit['id'],expected_request_id=audit['request_id'])
        add(_base(scope,'change:'+str(audit['id']),'memory_retirement_audit+receipts',audit['created_at'],kind,
            operation=operation,state='completed'if complete else'audit_only',memory_id=audit['memory_id'],
            request_id=audit['request_id'],audit_id=audit['id'],
            reason_code=_text(part.get('reason_code')or operation,80),reason=_text(audit['reason']),
            actor=part.get('actor')or whole.get('actor','unknown'),reversible=bool(part.get('reversible',whole.get('reversible',False))),
            completion=dict(audit_present=True,receipt_present=complete,
                audit=dict(id=audit['id'],request_id=audit['request_id']),
                receipt=dict(request_id=audit['request_id'])if complete else None),
            restore=dict(allowed=bool(capability.get('allowed')),route=capability.get('route'),
                         reason_code=capability.get('reason_code')),operator=_text(audit['operator'],120)))
    for(request_id,mid),(whole,part)in receipt_map.items():
        if(request_id,mid)in used:continue
        # A receipt outside the requested time window is not an unmatched in-window action.
        at=part.get('at',whole.get('at',whole.get('created_at')))
        if at is None or not opts['start']<=float(at)<opts['end']:continue
        if any(a['request_id']==request_id and a['memory_id']==mid for a in audits):continue
        issues['receipt_without_audit']+=1
        add(_base(scope,'receipt:'+request_id+':'+str(mid),'memory_retirement_receipts',at,
            'delete'if part.get('operation')in('soft','hard','purge')else'archive',
            operation=part.get('operation',whole.get('operation')),state='receipt_only',memory_id=mid,request_id=request_id,
            completion=dict(audit_present=False,receipt_present=True),restore=dict(allowed=False,route=None,reason_code='audit_missing'),
            reason='仅找到回执，缺少可配对的审计'))
    if'memory_delete_log'in names:
        from .delete_audit import scope_digest
        completed=rows('memory_delete_log',"SELECT id,memory_id,created_at,actor FROM memory_delete_log "
            "WHERE mode='purge_completed' AND created_at>=? AND created_at<? AND json_valid(source) "
            "AND json_extract(source,'$.scope_digest')=?",(opts['start'],opts['end'],scope_digest(scope)))
        for row in completed:
            pending=conn.execute("SELECT id FROM memory_delete_log WHERE memory_id=? AND id<? AND mode='purge_pending' "
                "AND json_valid(source) AND json_extract(source,'$.scope_digest')=? ORDER BY id DESC LIMIT 1",
                (row['memory_id'],row['id'],scope_digest(scope))).fetchone()
            add(_base(scope,'purge:'+str(row['id']),'memory_delete_log',row['created_at'],'delete',
                operation='purge',state='completed',memory_id=row['memory_id'],operator=_text(row['actor'],120),
                reason_code='purge_completed',reason='既有离线彻底清理完成记录；只代表原管理范围',
                completion=dict(audit_present=bool(pending),receipt_present=True,audit_id=pending[0]if pending else None,
                    receipt_id=row['id'],correlation='same_memory_scoped_log_sequence_no_request_id'),
                request_id=None,restore=dict(allowed=False,route=None,reason_code='purge_irreversible')))
    return events,dict(issues),sources

def read(memory,data,*,summary_only=False,clock=time.time):
    scope=current(data).scope;opts=_options(data,scope,clock())
    try:
        with memory.lock,memory.store._lock:events,issues,sources=_load(memory,scope,opts)
    except sqlite3.Error:raise PanelError('ledger_read_unavailable',503)from None
    filtered=[event for event in events if not opts['kind']or event['kind']in opts['kind']]
    filtered.sort(key=lambda item:(item['at'],item['event_id']),reverse=True)
    summary=dict(total=len(filtered),by_kind=dict(Counter(e['kind']for e in filtered)),
        by_state=dict(Counter(e['state']for e in filtered if e['kind']=='judge')),
        count_meaning='本范围、本时间窗、成功记录且可投影的事件数；不是判断准确率',
        environment_unknown=sum(e['environment']=='unknown'for e in filtered),
        completeness='unknown',source_status=sources,issues=issues,
        evaluation=dict(status='not_connected',accuracy=None,holdout_count=None))
    result=dict(scope=scope,summary=summary,time_window=dict(after=opts['start'],before=opts['end'],as_of=opts['as_of'],
        timezone='Asia/Shanghai',time_meaning='记账时间；失败任务为当前状态更新时间'),
        coverage_notice='仅展示已成功写账的记录；未记账、未映射阶段及演练性质不能从空白推断。',
        evaluation_notice='本面板没有接入题库版本和留出划分；不显示准确率或把未知留出数写成0。')
    if summary_only:return result
    remaining=[e for e in filtered if opts['last']is None or(e['at'],e['event_id'])<opts['last']]
    page=remaining[:opts['limit']];next_cursor=None
    if len(remaining)>len(page):
        tail=page[-1]
        next_cursor=base64.urlsafe_b64encode(json.dumps(dict(v=1,binding=opts['binding'],as_of=opts['as_of'],
            last=[tail['at'],tail['event_id']]),separators=(',',':')).encode()).decode().rstrip('=')
    return dict(result,items=page,next_cursor=next_cursor,has_more=next_cursor is not None)

def handle(memory,method,path,data):
    try:
        if method=='GET'and path in('/api/console/ledger/events','/api/console/ledger/summary'):
            return 200,dict(status='ok',data=read(memory,data,summary_only=path.endswith('/summary')))
        if method=='POST'and path=='/api/console/ledger/restore':
            from .operation_ledger import restore_from_panel
            return 200,dict(status='ok',data=restore_from_panel(memory,data))
        return 404,dict(status='error',error='unknown_ledger_route')
    except (ValueError,TypeError)as error:
        return getattr(error,'code',400),dict(status='error',error=getattr(error,'error','invalid_ledger_request'))
    except sqlite3.Error:
        return 503,dict(status='error',error='ledger_write_unavailable'if method=='POST'else'ledger_read_unavailable')
    except Exception as error:
        try:print('[ledger_panel_error] '+json.dumps(dict(error_type=type(error).__name__,method=method)),flush=True)
        except (OSError,ValueError):pass
        return 503,dict(status='error',error='ledger_operation_unavailable')
