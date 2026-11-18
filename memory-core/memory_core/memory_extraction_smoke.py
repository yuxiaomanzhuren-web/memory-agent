"""The user's four originals through the real extraction commit, isolated storage only."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import time

ORIGINALS = (
    ('1794551556000-9917', 1794551556, '我爱吃玉米'),
    ('1794551573000-1684', 1794551573, '刚刚说错了 其实玉米一点也不好吃'),
    ('1794551626000-8686', 1794551626, '说错了 生啃一点不好吃'),
    ('1794551693000-6161', 1794551693, '生啃甜玉米其实好吃的'),
)


def merged_output(originals=ORIGINALS):
    return dict(items=[dict(text='用户先说自己爱吃玉米，紧接着立刻改口说刚才讲错了，其实玉米一点也不好吃',
        kind='preference', importance=.6, topic='玉米偏好', object='玉米', action='说错', scene='',
        source_refs=[dict(raw_id=r[0],quote=r[2]) for r in reversed(originals[:2])], links=[])])


@contextmanager
def fixture(originals=ORIGINALS, output=None):
    from .server import MemoryService
    from .memory_pipeline import MemoryPipeline
    from .memory_temporal import migrate
    with tempfile.TemporaryDirectory(prefix='round12-extraction-') as temp:
        root=Path(temp)
        memory=MemoryService(str(root/'brain.db'),embed=False)
        try:
            raw=root/'raw'
            raw.mkdir(exist_ok=True)
            memory.raw.root=raw
            scope=dict(bot_id='991111',persona='round12-fixture',conv_key='private:992222')
            archive=raw/'2026-11.jsonl'
            archive.write_text(''.join(json.dumps(dict(id=rid,ts=ts,s='991111|private:992222',r='user',t=text),ensure_ascii=False)+'\n'
                                       for rid,ts,text in originals),encoding='utf-8')
            now=max(time.time(), originals[-1][1]+1)
            model=lambda *_: merged_output(originals) if output is None else output
            pipeline=MemoryPipeline(memory,model=model,clock=lambda:now,batch_size=1,idle_sec=0)
            conn=pipeline.conn
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                migrate(conn)
            pipeline.notice(dict(scope,raw_ids=[r[0] for r in originals]))
            yield dict(memory=memory,pipeline=pipeline,conn=conn,scope=scope,archive=archive,now=now)
        finally:
            memory.store.close()


def run_smoke():
    from .memory_temporal import TemporalFacts
    from .memory_extraction_versions import warnings_for
    started=time.monotonic()
    with fixture() as f:
        conn,memory=f['conn'],f['memory']
        before=f['archive'].read_bytes()
        result=f['pipeline'].tick()
        assert 'error' not in result, result
        assert result['created']==3, result
        rows=[dict(r) for r in conn.execute('SELECT * FROM memories ORDER BY id')]
        assert len(rows)==3
        mids=[r['id'] for r in rows]
        refs=[json.loads(r['meta'])['source_refs'] for r in rows]
        assert [[r['raw_id'] for r in group] for group in refs]==[[ORIGINALS[0][0]], [ORIGINALS[1][0],ORIGINALS[2][0]], [ORIGINALS[3][0]]]
        assert '生啃甜玉米' in rows[2]['text']
        assert all(not warnings_for(r['text']) for r in rows)
        edges=[dict(r) for r in conn.execute("SELECT * FROM memory_links WHERE relation='supersede' ORDER BY id")]
        assert len(edges)==2 and all(r['stance_tag']=='transition' for r in edges)
        assert [(r['a_id'],r['b_id']) for r in edges]==list(zip(mids,mids[1:]))
        assert edges[0]['weight']==.25 and edges[0]['superseded_by']==edges[1]['id']
        assert edges[1]['weight']==1 and edges[1]['superseded_by'] is None
        for r in edges:
            meta=conn.execute('SELECT source_refs FROM memory_pipeline_link_meta WHERE link_id=?',(r['id'],)).fetchone()
            assert all(ref['quote'] in r['reason'] for ref in json.loads(meta[0]))
            assert '迁移缺省值' in r['reason']
        assert f['archive'].read_bytes()==before
        assert all(memory.store.get_memory(mid)['id']==mid for mid in mids)
        chain=TemporalFacts(memory).chain(dict(f['scope'],fact_key='extracted|玉米|口味',include_history=True))
        assert [r['memory_id'] for r in chain['items']]==mids
        assert chain['items'][0]['superseded_memory_id']==mids[1]
        assert chain['items'][1]['superseded_memory_id']==mids[2]
        assert len(chain['items'][1]['source_refs'])==2
        current=memory.engine.recall('玉米',k=20,touch=False,rerank=False)
        assert [r['id'] for r in current if r['id'] in mids]==[mids[-1]],current
        assert f['pipeline'].tick().get('idle') is True
        assert conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]==3
        return dict(result='EXTRACTION_VERSIONS_SMOKE_OK',isolated=True,created=3,
                    transition_edges=2,state_edges=3,memory_ids=mids,
                    transition_ids=[r['id'] for r in edges],
                    texts=[r['text'] for r in rows],raw_ids=[[r['raw_id'] for r in group] for group in refs],
                    checks=dict(three_endpoints=True,two_transitions=True,raw_bytes_unchanged=True,
                                by_id_retained=True,no_arc_in_text=True,qualified_object_retained=True,
                                current_only=True,replay_no_duplicates=True),
                    elapsed_seconds=round(time.monotonic()-started,3))
