"""Explicit fact versions. Migration never infers supersession from an old link."""
from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
from datetime import datetime,timedelta,timezone
import json
import math
import re
import sqlite3
import time
from types import SimpleNamespace

from .memory_evidence import (_metadata, _verify_record, owns_memory, raw_records,
                              raw_session, scope_from, scope_key)

LINK_COLUMNS = dict(t_valid='REAL', t_invalid='REAL', t_created='REAL', t_expired='REAL',
                    superseded_by='INTEGER', reason="TEXT NOT NULL DEFAULT ''",
                    stance_tag="TEXT NOT NULL DEFAULT 'active'", weight='REAL NOT NULL DEFAULT 1.0')
COST_COLUMNS = dict(resolved_at='REAL', resolved_by='TEXT')
DDL = [
    '''CREATE TABLE IF NOT EXISTS memory_fact_versions(
       scope TEXT NOT NULL,fact_key TEXT NOT NULL,memory_id INTEGER NOT NULL,
       link_id INTEGER NOT NULL,source_raw_id TEXT NOT NULL,source_ref TEXT NOT NULL,
       PRIMARY KEY(scope,memory_id),UNIQUE(link_id))''',
    '''CREATE TABLE IF NOT EXISTS memory_version_events(
       id INTEGER PRIMARY KEY,scope TEXT NOT NULL,request_id TEXT NOT NULL,
       operation TEXT NOT NULL,payload TEXT NOT NULL,result TEXT NOT NULL,
       created_at REAL NOT NULL,UNIQUE(scope,request_id))''',
    '''CREATE INDEX IF NOT EXISTS idx_fact_versions_key ON memory_fact_versions(scope,fact_key)''',
    '''CREATE TRIGGER IF NOT EXISTS memory_links_temporal_insert AFTER INSERT ON memory_links
       WHEN NEW.t_valid IS NULL OR NEW.t_created IS NULL BEGIN
       UPDATE memory_links SET t_valid=COALESCE(NEW.t_valid,NEW.created_at),
         t_created=COALESCE(NEW.t_created,NEW.created_at) WHERE id=NEW.id; END''',
]
ARC = re.compile(r'改口|纠正|翻篇')
ARC_PERMISSION = {
    '改口':re.compile(r'改口(?:了|[：:，,。]|$)|(?:已经|现在)改口'),
    '纠正':re.compile(r'(?:纠正|更正)(?:一下|了|[：:，,。]|$)|刚才说错了|其实是|不是.{1,40}而是'),
    '翻篇':re.compile(r'翻篇(?:了|[：:，,。]|$)|这事过去了|这件事过去了'),
}
CHANGE = re.compile('|'.join(p.pattern for p in ARC_PERMISSION.values())+r'|不用再提|不必再提|别再提')
CLOSED = re.compile(r'这事过去了|这件事过去了|不用再提|不必再提|别再提|已经了结|这事了结了')
UNCERTAIN_CHANGE = re.compile(r'不确定|也许|可能|好像|似乎|如果|假如|假设')
PLANNED_CHANGE = re.compile(r'(?:希望|要求|请|打算|准备|明天|以后|将来|将要)[^，,。；;\n]{0,20}(?:改口|纠正|更正|翻篇)')


def denied_change(text,anchor):
    # Negating a fact ("咖啡没有加糖") is different from denying a revision.
    bridge=r'(?:(?:说|表示|要求|想|要|决定|主动|明确|承认|真的|打算|这事|这件事|我|你|他|她|已经|又|刚才|现在|在|上|过|是|'+re.escape(anchor)+r')|\s){0,8}'
    return bool(re.search(r'(?:没有|没|并未|并非|不是|未曾|从未|不曾|尚未|未|不会|不想|不愿|还没|不要|不准|不许|不能|不可|不代表|不意味着|不等于|不同意|不认同|别|不)'+bridge+
                          r'(?:改口|纠正|更正|翻篇|过去了|不用再提|不必再提|别再提|了结)',text,re.I))


def _dump(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def _time_label(value):
    return '未结束' if value is None else datetime.fromtimestamp(value,timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def _columns(conn, table):
    return {r[1] for r in conn.execute('PRAGMA table_info('+table+')')}


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fact_versions'").fetchone() is not None


def migration_plan(conn):
    columns=_columns(conn,'memory_links')
    if not columns:
        raise ValueError('memory_links table is missing')
    pending = (conn.execute('SELECT COUNT(*) FROM memory_links WHERE t_valid IS NULL OR t_created IS NULL').fetchone()[0]
               if {'t_valid','t_created'} <= columns else conn.execute('SELECT COUNT(*) FROM memory_links').fetchone()[0])
    costcols=_columns(conn,'memory_gate_costs')
    return dict(links_total=conn.execute('SELECT COUNT(*) FROM memory_links').fetchone()[0],
                links_to_initialize=pending,
                link_columns_to_add=sorted(set(LINK_COLUMNS)-columns),
                cost_columns_to_add=sorted(set(COST_COLUMNS)-costcols),
                cost_records=conn.execute('SELECT COUNT(*) FROM memory_gate_costs').fetchone()[0] if costcols else 0)


def migrate(conn):
    """Caller owns a transaction and prints migration_plan before calling."""
    if not conn.in_transaction:
        raise ValueError('migration requires an explicit transaction')
    plan=migration_plan(conn)
    for name, declaration in LINK_COLUMNS.items():
        if name not in _columns(conn,'memory_links'):
            conn.execute('ALTER TABLE memory_links ADD COLUMN '+name+' '+declaration)
    conn.execute('''CREATE TABLE IF NOT EXISTS memory_gate_costs(
        scope TEXT NOT NULL,memory_id INTEGER NOT NULL,cost_type TEXT NOT NULL,
        raw_id TEXT NOT NULL,quote TEXT NOT NULL,reason TEXT NOT NULL,operator TEXT NOT NULL,
        PRIMARY KEY(scope,memory_id))''')
    for name, declaration in COST_COLUMNS.items():
        if name not in _columns(conn,'memory_gate_costs'):
            conn.execute('ALTER TABLE memory_gate_costs ADD COLUMN '+name+' '+declaration)
    conn.execute('''UPDATE memory_links SET t_valid=COALESCE(t_valid,created_at),
                    t_created=COALESCE(t_created,created_at)
                    WHERE t_valid IS NULL OR t_created IS NULL''')
    for sql in DDL:
        conn.execute(sql)
    return plan


def wants_history(data):
    if isinstance(data,str):
        return any(term in data for term in ('前因后果','完整版本链','历史版本','版本变迁'))
    return data.get('include_history') is True or wants_history(str(data.get('text',data.get('query',''))))


def current_link_sql(conn, alias='l', now=None):
    if not available(conn):
        return '', []
    return (f" AND {alias}.stance_tag IN ('active','transition') AND {alias}.superseded_by IS NULL"
            f' AND {alias}.t_valid<=? AND ({alias}.t_invalid IS NULL OR {alias}.t_invalid>?)'
            f' AND {alias}.t_created<=? AND {alias}.t_expired IS NULL', [time.time() if now is None else now]*3)


def _versions(conn):
    if not available(conn):
        return []
    cur=conn.execute('SELECT v.*,l.t_valid,l.t_invalid,l.t_created,l.t_expired,l.stance_tag,l.superseded_by,l.reason,l.weight '
                     'FROM memory_fact_versions v JOIN memory_links l ON l.id=v.link_id')
    names=[c[0] for c in cur.description]
    return [dict(zip(names,row)) for row in cur]


def filter_memories(conn, rows, *, include_history=False, now=None):
    """Before top-k, not after it. Unversioned memories retain their old behavior."""
    if not rows:
        return rows
    versions=_versions(conn)
    if not versions:
        return rows
    now=time.time() if now is None else now
    by_id={v['memory_id']:v for v in versions}
    current={}
    for v in versions:
        if (v['stance_tag'] in ('active','transition') and v['superseded_by'] is None
                and v['t_valid']<=now and v['t_created']<=now
                and (v['t_invalid'] is None or v['t_invalid']>now) and v['t_expired'] is None):
            key=(v['scope'],v['fact_key'])
            if key not in current or (v['t_valid'],v['t_created'],v['link_id']) > (current[key]['t_valid'],current[key]['t_created'],current[key]['link_id']):
                current[key]=v
    keep={v['memory_id'] for v in current.values()}
    out=[]
    for row in rows:
        version=by_id.get(row.get('id'))
        if version is not None and not include_history and row['id'] not in keep:
            continue
        hit=dict(row)
        if version is not None:
            already_weighted = isinstance(row.get('fact_version'),dict) and row['fact_version'].get('link_id')==version['link_id']
            hit['fact_version']=version
            if 'meta' in hit and not include_history:
                meta=dict(_metadata(hit))
                primary=json.loads(version['source_ref'])
                support=primary.pop('version_support_refs',[])
                refs=[primary]+support
                meta['source_refs']=refs
                if refs:
                    meta.update({k:refs[0][k] for k in ('raw_id','raw_ts','quote') if k in refs[0]})
                hit['meta']=_dump(meta) if isinstance(row['meta'],str) else meta
            if 'score' in hit and not already_weighted:
                hit['score']=float(hit['score'])*float(version['weight'])
        out.append(hit)
    return out


def hidden_raw_ids(conn):
    versions=_versions(conn)
    if not versions:
        return set()
    ids={v['memory_id'] for v in versions}
    current={h['id'] for h in filter_memories(conn,[dict(id=i) for i in ids])}
    hidden,visible=set(),set()
    for mid in ids:
        row=conn.execute('SELECT meta FROM memories WHERE id=?',(mid,)).fetchone()
        meta=_metadata({'meta':row[0]}) if row else {}
        refs=meta.get('source_refs') or [{'raw_id':meta.get('raw_id')}]
        if mid in current:
            visible.update(v['source_raw_id'] for v in versions if v['memory_id']==mid)
        else:
            hidden.update(r['raw_id'] for r in refs if isinstance(r,dict) and r.get('raw_id'))
    return hidden-visible


def retained_memory(conn,mid):
    if not available(conn):
        return False
    return conn.execute('''SELECT 1 FROM memory_fact_versions v JOIN memory_links l ON l.id=v.link_id
        WHERE v.memory_id=? OR (l.a_kind='memory' AND l.a_id=?) OR (l.b_kind='memory' AND l.b_id=?) LIMIT 1''',
        (int(mid),int(mid),int(mid))).fetchone() is not None


def _text(data,key,limit=300):
    value=data.get(key)
    if not isinstance(value,str) or not value.strip() or len(value)>limit:
        raise ValueError('invalid '+key)
    return value.strip()


def _id(data,key):
    value=data.get(key)
    if type(value) is not int or value<=0:
        raise ValueError('invalid '+key)
    return value


def _stamp(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
        raise ValueError('timestamps must be finite Unix seconds')
    return float(value)


def joint_transition(older,newer,originals,object_anchor,required=()):
    base=bool(older['role']=='user' and newer['role']=='user'
                and older['raw_id']!=newer['raw_id'] and older['raw_ts']<newer['raw_ts']
                and isinstance(object_anchor,str) and object_anchor.strip()
                and object_anchor not in ('我','你','他','她','它','东西','事情','喜欢','讨厌','对象','状态','偏好')
                and object_anchor in older['quote'] and object_anchor in newer['quote'])
    if not base:
        return False
    from .memory_gates import _source_stance,_statement_parts
    for ref in (older,newer):
        text=originals[ref['raw_id']]
        if (_source_stance(dict(r='user',t=text))['support_scope']!='可作主张依据'
                or not any(object_anchor in s and s.rstrip('。！？?!；;\n').strip() in ref['quote'] for s in _statement_parts(text))):
            return False
    text=originals[newer['raw_id']]
    from .memory_extraction_versions import counterclaim
    return bool((CHANGE.search(newer['quote']) or counterclaim(older['quote'],newer['quote'])) and not UNCERTAIN_CHANGE.search(text)
                and not PLANNED_CHANGE.search(text) and not denied_change(text,object_anchor)
                and all(ARC_PERMISSION[word].search(newer['quote']) for word in required))


class TemporalFacts:
    def __init__(self,memory,clock=time.time):
        self.memory,self.clock=memory,clock

    @contextmanager
    def _connection(self,write=False):
        with closing(sqlite3.connect(str(self.memory.store.path),timeout=2)) as conn:
            conn.row_factory=sqlite3.Row
            if not available(conn):
                raise ValueError('temporal migration required; run the preview/apply script first')
            if write:
                conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
                if write:
                    conn.commit()
            except BaseException:
                if write:
                    conn.rollback()
                raise

    def _memory(self,conn,scope,mid):
        row=conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()
        if row is None or not owns_memory(SimpleNamespace(store=SimpleNamespace(conn=conn)),row,scope):
            raise ValueError('fact memory outside scope')
        return dict(row)

    def _edge(self,conn,scope,lid,mid):
        row=conn.execute('SELECT * FROM memory_links WHERE id=?',(lid,)).fetchone()
        if row is None or row['a_kind']!='memory' or row['b_kind']!='memory' or mid not in (row['a_id'],row['b_id']):
            raise ValueError('link must contain its explicit fact memory')
        self._memory(conn,scope,row['a_id'])
        self._memory(conn,scope,row['b_id'])
        table=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_pipeline_link_meta'").fetchone()
        meta=conn.execute('SELECT scope,state FROM memory_pipeline_link_meta WHERE link_id=?',(lid,)).fetchone() if table else None
        if meta is not None and (meta['scope']!=scope_key(scope) or meta['state']!='active'):
            raise ValueError('removed or foreign link cannot become a fact version')
        return dict(row)

    def _source(self,scope,ref,records):
        checked=_verify_record(scope,ref,records)
        with self.memory.lock, self.memory.store._lock:
            if not available(self.memory.store.conn):
                raise ValueError('temporal migration required')
            index=self.memory.store.conn.execute('SELECT * FROM memory_pipeline_raw WHERE raw_id=?',(checked['raw_id'],)).fetchone()
        rec=records[checked['raw_id']]
        if (index is None or index['scope']!=scope_key(scope) or index['session']!=raw_session(scope)
                or index['sha256']!=rec['sha256'] or index['file']!=rec['file']
                or index['offset']!=rec['offset'] or index['size']!=rec['size'] or index['role']!=rec['r']
                or float(index['ts'])!=float(rec['ts'])):
            raise ValueError('source is not byte-verified and assigned to this persona scope')
        from .memory_gates import _source_stance,_statement_parts
        if (checked['role']!='user' or _source_stance(rec)['support_scope']!='可作主张依据'
                or not any(s.rstrip('。！？?!；;\n').strip() in checked['quote'] for s in _statement_parts(rec['t']))):
            raise ValueError('version evidence must be a qualified user statement, not a paste or question')
        return checked

    def _previous(self,conn,scope,data,operation):
        rid=_text(data,'request_id',160)
        old=conn.execute('SELECT * FROM memory_version_events WHERE scope=? AND request_id=?',(scope_key(scope),rid)).fetchone()
        if old:
            if old['operation']!=operation or old['payload']!=_dump(data):
                raise ValueError('request_id already used for another operation')
            return json.loads(old['result'])

    def _event(self,conn,scope,data,operation,result):
        conn.execute('INSERT INTO memory_version_events(scope,request_id,operation,payload,result,created_at) VALUES(?,?,?,?,?,?)',
                     (scope_key(scope),_text(data,'request_id',160),operation,_dump(data),_dump(result),self.clock()))

    def supersede(self,data,*,connection=None):
        scope=scope_from(data)
        oldid,newid=_id(data,'old_link_id'),_id(data,'new_link_id')
        oldmid,newmid=_id(data,'old_memory_id'),_id(data,'new_memory_id')
        key,reason=_text(data,'fact_key',160),_text(data,'reason')
        _text(data,'operator',80)
        if oldid==newid or oldmid==newmid or any(term in reason for term in ('新信息优先','自动失效')):
            raise ValueError('distinct versions and a specific evidence-backed reason are required')
        tag=data.get('stance_tag','active')
        if tag not in ('active','transition'):
            raise ValueError('new version must be active or transition')
        refs=data.get('source_refs')
        if not isinstance(refs,list) or len(refs)!=2:
            raise ValueError('two endpoint quotations are required; chronology alone only supports 先后发生')
        records=raw_records(self.memory.raw.root,raw_session(scope))
        refs=[self._source(scope,r,records) for r in refs]
        with (self._connection(True) if connection is None else nullcontext(connection)) as conn:
            if not conn.in_transaction:
                raise ValueError('supersede requires a transaction')
            previous=self._previous(conn,scope,data,'supersede')
            if previous is not None:
                return previous
            old=self._edge(conn,scope,oldid,oldmid)
            new=self._edge(conn,scope,newid,newmid)
            oldmeta=_metadata(self._memory(conn,scope,oldmid))
            newmeta=_metadata(self._memory(conn,scope,newmid))
            oldraw={r.get('raw_id') for r in oldmeta.get('source_refs',[])}
            newraw={r.get('raw_id') for r in newmeta.get('source_refs',[])}
            older=[r for r in refs if r['raw_id'] in oldraw]
            newer=[r for r in refs if r['raw_id'] in newraw-oldraw]
            if len(older)!=1 or len(newer)!=1 or older[0]['raw_ts']>=newer[0]['raw_ts']:
                raise ValueError('need distinct older/newer user quotations from the two fact memories')
            older,newer=older[0],newer[0]
            anchor=data.get('object_anchor') or oldmeta.get('object') or newmeta.get('object')
            if (not isinstance(anchor,str) or not anchor.strip() or len(anchor)>80
                    or anchor in ('我','你','他','她','它','东西','事情','喜欢','讨厌','对象','状态','偏好')
                    or anchor not in older['quote'] or anchor not in newer['quote']):
                raise ValueError('两处原话缺少共同具体对象；只能说先后发生，不能把无关事实替代')
            # A timestamp order licenses no change-of-mind verb on its own.
            narrative=re.sub(r'「[^」]*」|“[^”]*”', '', reason)
            required=set(ARC.findall(narrative))
            if new['relation']=='纠正了':
                required.add('纠正')
            originals={r['raw_id']:records[r['raw_id']]['t'] for r in (older,newer)}
            licensed=joint_transition(older,newer,originals,anchor,required)
            if (tag=='transition' or ARC.search(narrative) or new['relation']=='纠正了') and not licensed:
                raise ValueError('pair does not jointly license 改口/纠正/翻篇; only 先后发生 is supported')
            now=self.clock()
            if 't_valid' not in data:
                raise ValueError('t_valid is required: source publication time is not world effective time')
            valid=_stamp(data['t_valid'])
            oldstart=_stamp(data.get('old_t_valid',old['t_valid']))
            if oldstart!=old['t_valid'] and conn.execute('SELECT 1 FROM memory_fact_versions WHERE link_id=?',(oldid,)).fetchone():
                raise ValueError('an established version start cannot be rewritten; retain its audit history')
            if not oldstart<=valid<=now:
                raise ValueError('valid interval is inverted or future-dated; an initial legacy placeholder needs an explicit old_t_valid')
            if old['superseded_by'] is not None or old['t_expired'] is not None or old['stance_tag']=='superseded':
                raise ValueError('old version already superseded; no branching or resurrection')
            if new['superseded_by'] is not None or new['t_expired'] is not None or new['stance_tag']=='superseded':
                raise ValueError('replacement is not a current version')
            existing=conn.execute('SELECT * FROM memory_fact_versions WHERE scope=? AND fact_key=?',(scope_key(scope),key)).fetchall()
            mapped={r['memory_id']:r['link_id'] for r in existing}
            if mapped and mapped.get(oldmid)!=oldid:
                raise ValueError('fact_key belongs to a different chain')
            if any(r['link_id']==newid or r['memory_id']==newmid for r in existing):
                raise ValueError('replacement already belongs to this chain')
            for mid,lid,ref in ((oldmid,oldid,older),(newmid,newid,newer)):
                found=conn.execute('SELECT * FROM memory_fact_versions WHERE memory_id=? OR link_id=?',(mid,lid)).fetchall()
                if any(r['scope']!=scope_key(scope) or r['fact_key']!=key or r['memory_id']!=mid or r['link_id']!=lid for r in found):
                    raise ValueError('fact memory or edge already assigned to another chain')
                conn.execute('INSERT OR IGNORE INTO memory_fact_versions VALUES(?,?,?,?,?,?)',
                             (scope_key(scope),key,mid,lid,ref['raw_id'],_dump(ref)))
            conn.execute("UPDATE memory_links SET t_valid=?,t_invalid=?,t_expired=?,superseded_by=?,reason=?,stance_tag='superseded',weight=0.25 WHERE id=?",
                         (oldstart,valid,now,newid,reason,oldid))
            conn.execute('UPDATE memory_links SET t_valid=?,t_invalid=NULL,t_expired=NULL,stance_tag=?,reason=?,weight=1.0 WHERE id=?',
                         (valid,tag,reason,newid))
            resolved=[]
            if tag=='transition':
                costs=conn.execute('SELECT * FROM memory_gate_costs WHERE scope=? AND memory_id=? AND resolved_at IS NULL',
                                   (scope_key(scope),oldmid)).fetchall()
                for cost in costs:
                    source=records.get(cost['raw_id'])
                    if (source is not None and newer['raw_ts']>float(source['ts'])
                            and new['t_created']>=float(source['ts'])):
                        conn.execute('UPDATE memory_gate_costs SET resolved_at=?,resolved_by=? WHERE scope=? AND memory_id=? AND resolved_at IS NULL',
                                     (now,'transition:'+str(newid),scope_key(scope),oldmid))
                        resolved.append(oldmid)
            result=dict(old_link_id=oldid,new_link_id=newid,old_memory_id=oldmid,new_memory_id=newmid,
                        fact_key=key,reason=reason,source_refs=refs,stance_tag=tag,
                        object_anchor=anchor,
                        t_valid=valid,old_t_valid=oldstart,old_t_valid_before=old['t_valid'],
                        narration='两处原话联合支持明确转变' if licensed else '先后发生',resolved_costs=resolved)
            self._event(conn,scope,data,'supersede',result)
            return result

    def resolve_cost(self,data):
        scope=scope_from(data)
        mid=_id(data,'memory_id')
        _text(data,'operator',80)
        reason=_text(data,'reason')
        records=raw_records(self.memory.raw.root,raw_session(scope))
        ref=self._source(scope,data.get('source_ref'),records)
        with self._connection(True) as conn:
            previous=self._previous(conn,scope,data,'resolve_cost')
            if previous is not None:
                return previous
            memory=self._memory(conn,scope,mid)
            obj=str(_metadata(memory).get('object') or '')
            cost=conn.execute('SELECT * FROM memory_gate_costs WHERE scope=? AND memory_id=?',(scope_key(scope),mid)).fetchone()
            if cost is None:
                raise ValueError('scoped cost record not found')
            source=records.get(cost['raw_id'])
            if (source is None or ref['raw_ts']<=float(source['ts']) or not obj or obj.lower() not in ref['quote'].lower()
                    or not CLOSED.search(ref['quote']) or UNCERTAIN_CHANGE.search(records[ref['raw_id']]['t'])
                    or denied_change(records[ref['raw_id']]['t'],obj)):
                raise ValueError('resolution requires a later explicit user closure naming this object; elapsed time is insufficient')
            if cost['resolved_at'] is not None:
                raise ValueError('cost is already resolved; use the original request_id for an idempotent replay')
            now=self.clock()
            conn.execute('UPDATE memory_gate_costs SET resolved_at=?,resolved_by=? WHERE scope=? AND memory_id=?',
                         (now,'raw:'+ref['raw_id'],scope_key(scope),mid))
            result=dict(memory_id=mid,resolved_at=now,resolved_by='raw:'+ref['raw_id'],source_ref=ref,reason=reason)
            self._event(conn,scope,data,'resolve_cost',result)
            return result

    def chain(self,data):
        scope=scope_from(data)
        key=_text(data,'fact_key',160)
        history=wants_history(data)
        records=raw_records(self.memory.raw.root,raw_session(scope))
        with self._connection() as conn:
            rows=[dict(r) for r in conn.execute('SELECT l.*,v.memory_id,v.source_raw_id,v.source_ref FROM memory_links l JOIN memory_fact_versions v ON v.link_id=l.id '
                                              'WHERE v.scope=? AND v.fact_key=? ORDER BY l.t_created,l.id',(scope_key(scope),key))]
            if not rows:
                return dict(items=[],rendered='',include_history=history)
            if not history:
                current={r['id'] for r in filter_memories(conn,[dict(id=r['memory_id']) for r in rows])}
                rows=[r for r in rows if r['memory_id'] in current]
            events=[dict(r) for r in conn.execute("SELECT * FROM memory_version_events WHERE scope=? AND operation='supersede' ORDER BY id",(scope_key(scope),))]
            events=[json.loads(e['result']) for e in events if json.loads(e['result']).get('fact_key')==key]
            lines=[]
            for row in rows:
                memory=self._memory(conn,scope,row['memory_id'])
                row['text']=memory['text']
                primary=json.loads(row['source_ref'])
                refs=[primary]+primary.pop('version_support_refs',[])
                row['superseded_memory_id']=next((r['memory_id'] for r in rows if r['id']==row['superseded_by']),None)
                try:
                    row['source_refs']=[self._source(scope,r,records) for r in refs]
                except ValueError as exc:
                    row['source_refs']=[]
                    row['source_error']=str(exc)
                row['replacement_evidence']=[e for e in events if e['old_link_id']==row['id']]
                refs='；'.join(f"{r['raw_id']}：{r['quote']}" for r in row['source_refs'] if 'raw_id' in r and 'quote' in r) or '原话当前不可核验'
                reason=row['reason'] or '尚未被取代'
                lines.append(f"边 #{row['id']} / 条目 #{row['memory_id']} [{row['stance_tag']}] {row['text']}；"
                             f"世界时间 {_time_label(row['t_valid'])} → {_time_label(row['t_invalid'])}；"
                             f"系统时间 {_time_label(row['t_created'])} → {_time_label(row['t_expired'])}；"
                             f"取代它的边={row['superseded_by']}；理由：{reason}；原话：{refs}")
            return dict(items=rows,rendered='\n'.join(lines),include_history=history)

    def status(self):
        with closing(sqlite3.connect(str(self.memory.store.path))) as conn:
            return dict(ready=available(conn),migration=migration_plan(conn))

    def cost(self,data):
        scope=scope_from(data)
        mid=int(data.get('memory_id',0))
        if mid<=0:
            raise ValueError('memory_id required')
        with self._connection() as conn:
            self._memory(conn,scope,mid)
            row=conn.execute('SELECT * FROM memory_gate_costs WHERE scope=? AND memory_id=?',(scope_key(scope),mid)).fetchone()
            if row is None:
                raise ValueError('scoped cost not found')
            return dict(row)


def handle_request(memory,method,path,data):
    facts=TemporalFacts(memory)
    try:
        if method=='GET' and path=='/memory/versions/status':
            return 200,facts.status()
        if method=='GET' and path=='/memory/versions/chain':
            return 200,facts.chain(data)
        if method=='GET' and path=='/memory/versions/cost':
            return 200,facts.cost(data)
        if method=='POST' and path=='/memory/versions/supersede':
            return 200,facts.supersede(data)
        if method=='POST' and path=='/memory/versions/resolve-cost':
            return 200,facts.resolve_cost(data)
        if method=='POST' and path=='/memory/versions/smoke':
            from .memory_temporal_smoke import run_smoke
            return 200,run_smoke()
        if method=='POST' and path=='/memory/versions/extraction-smoke':
            from .memory_extraction_smoke import run_smoke
            return 200,run_smoke()
        return 404,dict(error='unknown temporal operation')
    except (ValueError,KeyError,TypeError,sqlite3.IntegrityError) as exc:
        return 400,dict(error=str(exc))
