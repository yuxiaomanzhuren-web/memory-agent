"""End-to-end checks in disposable SQLite/raw storage, never production state."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import time


@contextmanager
def fixture(clock=time.time):
    from .server import MemoryService
    from .memory_pipeline import MemoryPipeline
    from .memory_gates import MemoryGates
    from .memory_temporal import TemporalFacts,migrate
    with tempfile.TemporaryDirectory(prefix='temporal-facts-') as root:
        memory=MemoryService(str(Path(root)/'brain.db'),embed=False)
        try:
            pipeline=MemoryPipeline(memory,model=lambda *_: (_ for _ in ()).throw(AssertionError('no model')),clock=clock)
            conn=getattr(memory.store.conn,'raw',memory.store.conn)
            scope=dict(bot_id='991111',persona='temporal-fixture',conv_key='private:992222')
            now=clock()

            def remember(quote,stamp,text=None,**meta):
                stamp=round(stamp,3)
                rid=memory.raw.append('991111|private:992222','user',quote,ts=stamp)
                pipeline.notice({**scope,'raw_ids':[rid]})
                ref=dict(raw_id=rid,raw_ts=stamp,quote=quote)
                mid=memory.remember(dict(text=quote if text is None else text,kind='fact',created_at=stamp,
                    meta=dict(pipeline_scope=scope,source_refs=[ref],topic='咖啡偏好',**meta)))['id']
                conn.execute('INSERT INTO memory_pipeline_owners(memory_id,scope) VALUES(?,?)',
                             (mid,json.dumps(scope,ensure_ascii=False,sort_keys=True,separators=(',',':'))))
                conn.commit()
                return mid,ref

            anchor,_=remember('我记录咖啡偏好',now-1000)
            old,oldref=remember('我喝咖啡加糖，上次错过了无糖的选择',now-900,text='他喝咖啡加糖',object='咖啡',action='加糖')
            new,newref=remember('我改口了，咖啡现在不加糖',now-700,text='他喝咖啡不加糖',object='咖啡',action='不加糖')
            neutral,neutralref=remember('我喝咖啡不加糖',now-600,object='咖啡',action='不加糖')
            closed,closedref=remember('咖啡这事过去了，不用再提',now-500,object='咖啡',action='了结')
            links=[]
            for mid,created in zip((old,new,neutral),(now-850,now-650,now-550)):
                a,b=sorted((anchor,mid))
                c=conn.execute("INSERT INTO memory_links(a_id,b_id,relation,why,created_at) VALUES(?,?,'同话题','fixture source',?)",(a,b,created))
                lid=c.lastrowid
                conn.execute('INSERT INTO memory_pipeline_link_meta(link_id,scope,source_refs,operator,updated_at) VALUES(?,?,?,?,?)',
                             (lid,json.dumps(scope,ensure_ascii=False,sort_keys=True,separators=(',',':')),'[]','isolated',now))
                links.append(lid)
            conn.commit()
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                migrate(conn)
            gates=MemoryGates(memory,clock=clock)
            gates.cost({**scope,'memory_id':old,'cost_type':'错过','raw_id':oldref['raw_id'],
                        'reason':'用户原话写明错过无糖的选择','operator':'isolated'})
            proposal=dict(scope,request_id='replace-coffee',fact_key='咖啡|是否加糖',old_link_id=links[0],new_link_id=links[1],
                          old_memory_id=old,new_memory_id=new,source_refs=[oldref,newref],t_valid=now-700,
                          reason='新原话明确说咖啡现在不加糖，旧原话仍保留',operator='isolated',stance_tag='transition')
            yield dict(memory=memory,pipeline=pipeline,conn=conn,scope=scope,gates=gates,
                       facts=TemporalFacts(memory,clock=clock),old=old,new=new,neutral=neutral,closed=closed,
                       oldref=oldref,newref=newref,neutralref=neutralref,closedref=closedref,
                       links=links,proposal=proposal,remember=remember,now=now)
        finally:
            memory.store.close()


def run_smoke():
    from .memory_temporal import migrate,migration_plan
    started=time.monotonic()
    with fixture() as f:
        memory,conn,facts=f['memory'],f['conn'],f['facts']
        baseline=len(conn.execute('SELECT id FROM memory_links').fetchall())
        before=memory.engine.recall('咖啡',k=20,touch=False,rerank=False)
        assert {f['old'],f['new']} <= {r['id'] for r in before}
        allowed=f['gates'].three({**f['scope'],'text':'咖啡加糖重新讨论','context_ts':time.time(),
                                 'why_now':'现在提到咖啡加糖','request_id':'unresolved-positive-control'})
        assert allowed['allowed'] is True
        result=facts.supersede(f['proposal'])
        assert facts.supersede(f['proposal'])==result
        old=dict(conn.execute('SELECT * FROM memory_links WHERE id=?',(f['links'][0],)).fetchone())
        assert old['stance_tag']=='superseded' and old['weight']==0.25 and old['superseded_by']==f['links'][1]
        assert len(conn.execute('SELECT id FROM memory_links').fetchall())==baseline
        assert memory.store.get_memory(f['old']) is not None
        current=memory.engine.recall('咖啡',k=20,touch=False,rerank=False)
        assert f['old'] not in {r['id'] for r in current} and f['new'] in {r['id'] for r in current}
        assert sum(r['id'] in (f['old'],f['new']) for r in current)==1
        chain=facts.chain({**f['scope'],'fact_key':f['proposal']['fact_key'],'include_history':True})
        assert len(chain['items'])==2 and f['oldref']['raw_id'] in chain['rendered'] and f['newref']['raw_id'] in chain['rendered']
        denied=f['gates'].three({**f['scope'],'text':'咖啡加糖重新讨论','context_ts':time.time(),
                                 'why_now':'现在提到咖啡加糖','request_id':'resolved-cost-check'})
        assert denied['allowed'] is False and denied['reason_code']=='cost_resolved'
        plan=migration_plan(conn)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            migrate(conn)
        assert migration_plan(conn)['links_to_initialize']==0 and plan['links_to_initialize']==0
        assert dict(conn.execute('SELECT * FROM memory_links WHERE id=?',(f['links'][0],)).fetchone())==old
        return dict(result='TEMPORAL_SMOKE_OK',isolated=True,elapsed_seconds=round(time.monotonic()-started,3),
                    checks=dict(old_preserved=True,current_only=True,one_version_per_fact=True,
                                readable_chain=True,resolved_cost_denied=True,migration_idempotent=True),
                    gate3=denied,current_ids=[r['id'] for r in current])
