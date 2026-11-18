"""Credential-bound recall. No request string or legacy source prefix grants access."""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib,hmac,json,os,sqlite3,time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from .memory_evidence import scope_from,scope_key,owns_memory

ACCESS=ContextVar('memory_recall_access',default=None)

class ScopeError(ValueError):
    def __init__(self,error='scope_forbidden',code=403):
        super().__init__(error);self.error=error;self.code=code

@dataclass(frozen=True)
class Access:
    bot_id:str
    persona:str
    conv_key:str
    @property
    def scope(self):return dict(bot_id=self.bot_id,persona=self.persona,conv_key=self.conv_key)

def observe(branch,**extra):
    access=ACCESS.get()
    print('[scope_recall] '+json.dumps(dict(at=time.time(),branch=branch,scope_applied=True,authz='ok',
        scope=access.scope if isinstance(access,Access) else None,**extra),ensure_ascii=False,separators=(',',':')),flush=True)

def authorize(headers,data,*,legacy=False):
    if not isinstance(data,dict):raise ScopeError('scope_required',400)
    if data.get('legacy') not in (None,False):raise ScopeError()
    if legacy and not data.get('scope'):raise ScopeError('scope_required',400)
    if any(data.get(k) in (None,'') for k in ('bot_id','persona','conv_key')):raise ScopeError('scope_required',400)
    try:requested=scope_from(data)
    except (ValueError,TypeError):raise ScopeError('invalid_scope',400) from None
    if legacy or 'scope' in data:
        declared=data['scope']
        if isinstance(declared,str):
            try:declared=json.loads(declared)
            except ValueError:raise ScopeError() from None
        if declared!=requested:raise ScopeError()
    token=headers.get('X-Memory-Scope-Credential','')
    if not isinstance(token,str) or not 32<=len(token)<=256:raise ScopeError()
    try:
        default=Path(os.environ.get('MEMORY_DB',str(Path.home()/'.memory_core/brain.db'))).parent/'recall-scope-server.json'
        config=json.loads(Path(os.environ.get('MEMORY_SCOPE_CREDENTIALS_FILE',str(default))).read_text('utf-8-sig'))
        if not isinstance(config,dict) or config.get('version')!=1 or not isinstance(config.get('credentials'),list):raise ValueError()
        digest=hashlib.sha256(token.encode()).hexdigest();found=[]
        for item in config['credentials']:
            if not isinstance(item,dict) or not isinstance(item.get('sha256'),str):raise ValueError()
            scopes=[scope_from(s) for s in item['scopes']]
            if hmac.compare_digest(item['sha256'],digest):found.extend(scopes)
        if requested not in found:raise ScopeError()
    except (OSError,KeyError,ValueError,TypeError):raise ScopeError() from None
    return Access(**requested)

def current(data=None):
    access=ACCESS.get()
    if not isinstance(access,Access):raise ScopeError('scope_required',400)
    if isinstance(data,dict) and any(k in data for k in ('bot_id','persona','conv_key')):
        try:
            if scope_from(data)!=access.scope:raise ScopeError()
        except (ValueError,TypeError):raise ScopeError() from None
    return access

class ScopedStore:
    def __init__(self,store,access):
        if not isinstance(access,Access):raise ScopeError()
        self.base,self.access,self.conn=store,access,store.conn
        # ponytail: scan ownership for this small local corpus; use an indexed join if it outgrows memory.
        memory=type('ReadOwner',(),{'store':store})()
        self.memory=memory
        self.allowed={r['id'] for r in self.conn.execute("SELECT * FROM memories WHERE status='active'") if owns_memory(memory,r,access.scope)}
    def __getattr__(self,name):return getattr(self.base,name)
    def get_memory(self,mid):
        if mid not in self.allowed:return None
        row=self.base.get_memory(mid)
        return row if row and row['status']=='active' and owns_memory(self.memory,row,self.access.scope) else None
    def fts_search(self,query,limit=50):
        observe('fts')
        count=self.conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
        return [(mid,score) for mid,score in self.base.fts_search(query,max(1,count)) if mid in self.allowed][:limit]
    def all_vectors(self):
        observe('vectors');ids,matrix=self.base.all_vectors()
        if not ids:return [],matrix
        indices=[i for i,mid in enumerate(ids) if mid in self.allowed]
        return [ids[i] for i in indices],matrix[indices]
    def pinned_memories(self):
        observe('pin')
        return [r for r in self.base.pinned_memories() if r['id'] in self.allowed]
    def neighbor_memories(self,seeds,limit=30):
        observe('graph')
        seeds=[mid for mid in seeds if mid in self.allowed]
        count=self.conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
        return [mid for mid in self.base.neighbor_memories(seeds,max(1,count)) if mid in self.allowed][:limit]
    def entities_of(self,seeds):return self.base.entities_of([mid for mid in seeds if mid in self.allowed])
    def ids_by_source_scope(self,requested):
        scope=self.access.scope
        return self.allowed if requested in (scope['bot_id']+'|'+scope['conv_key'],'qq:'+scope['bot_id']+'|'+scope['conv_key']) else set()
    def touch(self,ids,**kwargs):return self.base.touch([mid for mid in ids if mid in self.allowed],**kwargs)

def recall(service,data):
    access=current(data)
    view=ScopedStore(service.store,access)
    from .engine import MemoryEngine
    engine=MemoryEngine(view,embed=service.engine.embed if data.get('use_embedding',True) else None)
    query=str(data.get('query') or data.get('text') or '').strip()
    try:k=max(1,min(20,int(data.get('k') or 5)))
    except (ValueError,TypeError):raise ScopeError('invalid_k',400) from None
    requested=data.get('visibility',['public'])
    if not isinstance(requested,list) or any(v not in ('private','public','group') for v in requested):raise ScopeError('invalid_visibility',400)
    scene={'public','private'} if access.conv_key.startswith('private:') else {'public','group'}
    visibility=set(requested)&scene
    opts=dict(k=k,visibility_allow=visibility,include_history=bool(data.get('include_history')),touch=False)
    hits=engine.recall(query,**opts) if query and visibility else []
    from .synonyms import variants
    observe('synonyms')
    for variant in variants(query,limit=1) if query and visibility else []:
        more=engine.recall(variant,**opts)
        observe('literal_fts')
        for mid,_ in view.fts_search(variant,limit=4):
            row=view.get_memory(mid)
            if row and row['visibility'] in visibility:more.append(dict(id=mid,text=row['text'],kind=row['kind'],importance=row['importance'],visibility=row['visibility'],created_at=row['created_at'],score=.94))
        hits.extend(more)
    # Every merge goes back through the same identity-bound view.
    unique={}
    for hit in hits:
        row=view.get_memory(hit['id'])
        if row and row['visibility'] in visibility:unique.setdefault(hit['id'],hit)
    from .memory_temporal import filter_memories,wants_history
    hits=filter_memories(view.conn,list(unique.values()),include_history=wants_history(data))[:k]
    if not data.get('read_only'):view.touch([h['id'] for h in hits])
    observe('profiles',result='withheld_unowned_global_profiles')
    # Entity profiles have no scoped provenance. They are not safe to forward.
    return dict(hits=hits,block='\n'.join(f"{i}. {h['text']}" for i,h in enumerate(hits,1)),profiles=[],
                scope=access.scope,scope_applied=True,authz='ok')

def recent(service,data):
    access=current(data);view=ScopedStore(service.store,access)
    vis=set(data.get('visibility') or ['public'])&({'public','private'} if access.conv_key.startswith('private:') else {'public','group'})
    k=max(1,min(20,int(data.get('k') or 6)))
    rows=[view.get_memory(mid) for mid in sorted(view.allowed,reverse=True)]
    sources=[str(s) for s in data.get('sources',[]) if str(s)]
    since=time.time()-float(data.get('hours') or 0)*3600 if data.get('hours') else 0
    rows=[r for r in rows if r and r['visibility'] in vis and r['created_at']>=since and
          (not sources or any(str(r.get('source') or '').startswith(s) for s in sources))]
    packed=[{k:r[k] for k in ('id','text','kind','importance','created_at','visibility','source')} for r in rows]
    observe('recent')
    return dict(items=packed[:k],prefs=[r for r in packed if r['kind']=='preference'][:4] if not sources else [],scope=access.scope,scope_applied=True,authz='ok')

SCOPED_POST={'/recall','/recall_scoped','/recall_for_turn','/memory/verify','/review/read','/recent','/raw/recent'}
BLOCKED_READ={'/graph','/conflicts','/api/console/memories','/api/console/memory','/api/console/raw','/api/console/recall','/api/console/logs','/api/console/overview','/api/console/wakes','/raw/search','/digest/profile'}

def requires_scope(path):
    return path in('/ledger/events','/api/console/ledger/events','/api/console/ledger/summary','/api/console/ledger/restore') or path in SCOPED_POST or path.startswith(('/api/console/panel/','/memory/gates','/memory/versions/','/pipeline/timeline','/pipeline/links','/daybook/recent'))


def _reject_console_query_credentials(handler, path, query):
    if path.startswith('/api/console/') and 'token' in parse_qs(query, keep_blank_values=True):
        handler._send(400, {'status':'error', 'error':'query_credentials_not_allowed'})
        return True
    return False


def post(handler):
    """Outer request boundary; reset ContextVar even on keep-alive/error paths."""
    from io import BytesIO
    request = urlsplit(handler.path)
    if _reject_console_query_credentials(handler, request.path, request.query):
        handler.close_connection = True
        return
    if not handler._auth():handler._send(401,{'status':'error','message':'unauthorized'});return
    path=handler.path.split('?',1)[0]
    if path in BLOCKED_READ:
        handler._send(410,{'status':'error','data':{'error':'unscoped_read_disabled'}});return
    try:
        n=int(handler.headers.get('Content-Length') or 0)
        if not 0<=n<=8*1024*1024:raise ScopeError('invalid_body_size',400)
        blob=handler.rfile.read(n);data=json.loads(blob or '{}')
        if not isinstance(data,dict):raise ScopeError('invalid_body',400)
        access=authorize(handler.headers,data,legacy=path=='/recall') if requires_scope(path) else None
        if path in ('/v1/chat/completions','/v1/agent/chat/completions','/agent/chat'):
            access=authorize(handler.headers,data)
        if path=='/recall_scoped':
            mode=data.get('mode','search')
            translated={'search':'/recall','turn':'/recall_for_turn','verify':'/memory/verify'}.get(mode)
            if translated is None:raise ScopeError('invalid_recall_mode',400)
            handler.path=translated
        original_stream=handler.rfile;handler.rfile=BytesIO(blob)
        token=ACCESS.set(access)
        try:
            if access:observe('request',route=path)
            return handler._post_legacy()
        finally:ACCESS.reset(token);handler.rfile=original_stream
    except ScopeError as exc:handler._send(exc.code,{'status':'error','data':{'error':exc.error}})
    except (ValueError,TypeError):handler._send(400,{'status':'error','data':{'error':'invalid_body'}})

def get(handler):
    request=urlsplit(handler.path)
    path=request.path
    # Only a nonsecret static shell is public. Data routes still need both gates.
    if path=='/console':
        if request.query:
            handler._send(400,{'status':'error','error':'console_query_not_allowed'});return
        return handler._console_shell()
    if _reject_console_query_credentials(handler, path, request.query):return
    if not handler._auth():handler._send(401,{'status':'error','message':'unauthorized'});return
    if path in BLOCKED_READ:
        handler._send(410,{'status':'error','data':{'error':'unscoped_read_disabled'}});return
    try:
        parsed=parse_qs(request.query)
        if requires_scope(path) and any(len(parsed.get(k,[]))>1 for k in ('bot_id','persona','conv_key','scope')):raise ScopeError()
        data={k:v[-1] for k,v in parsed.items()}
        access=authorize(handler.headers,data) if requires_scope(path) else None
        token=ACCESS.set(access)
        try:return handler._get_legacy()
        finally:ACCESS.reset(token)
    except ScopeError as exc:handler._send(exc.code,{'status':'error','data':{'error':exc.error}})
