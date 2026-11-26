"""Live-server probes in an isolated DB; no production quota or memory writes."""
import hashlib
import json
from pathlib import Path
import tempfile
import time

from .memory_gates import GATE1_RECENT_RECORDS, MemoryGates

ABSENT_QUERIES = (
    '你还记得波波鹦鹉吗？',
    '你还记得上次我说过海底灯塔吗？',
    '你还记得黑洞辐射的公式吗？',
)


def fixture(root):
    from .server import MemoryService
    from .memory_pipeline import MemoryPipeline
    memory = MemoryService(str(Path(root) / 'gates.db'), embed=False)
    pipeline = MemoryPipeline(memory, model=lambda *_: (_ for _ in ()).throw(AssertionError('model must not run')))
    scope = dict(bot_id='991111', persona='gate-smoke', conv_key='private:992222')
    session = '991111|private:992222'
    old = round(time.time() - 120, 3)
    rows = [('早饭吃了豆腐脑，特意要了咸口。', '', ''),
            ('手机换电池的预约错过了，我要重约。', '手机', '换电池'),
            ('哈哈哈哈哈哈，好好好好好好。', '', ''),
            ('没有额外安排，先整理一只旧盒子。', '', ''),
            ('我在读动力学笔记。', '', ''),
            ('讨论明天的温度。', '', ''),
            ('平板电脑换屏幕的预约还没定。', '平板电脑', '换屏幕')]
    ids, mids = [], []
    for i, (quote, obj, action) in enumerate(rows):
        rid = memory.raw.append(session, 'user', quote, ts=old + i,
                                extra={'message_id': 'fixture-' + str(i)})
        pipeline.notice({**scope, 'raw_ids': [rid]})
        tags = memory.raw.source_tags_for([rid], session)[0]
        mid = memory.remember(dict(text=quote, meta=dict(pipeline_scope=scope, object=obj, action=action,
            source_refs=[dict(tags, raw_ts=old+i, quote=quote)])))['id']
        ids.append(rid)
        mids.append(mid)
    for i in range(GATE1_RECENT_RECORDS):
        # Keep the archived evidence outside the current-message window.
        rid = memory.raw.append(session, 'user', '好。', ts=old + len(rows) + i)
        pipeline.notice({**scope, 'raw_ids': [rid]})
    gates = MemoryGates(memory)
    try:
        gates.cost({**scope, 'memory_id': mids[1], 'cost_type':'错过', 'raw_id':ids[1],
                    'reason':'原文明确说预约错过', 'operator':'isolated-smoke'})
    except BaseException:
        memory.store.close()
        raise
    return memory, gates, scope, ids, mids


def run_smoke():
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='memory-gates-smoke-') as root:
        memory, gates, scope, ids, mids = fixture(root)
        try:
            found = gates.one({**scope, 'text':'你还记得早饭吃了豆腐脑吗'})
            suspected = gates.one({**scope, 'text':'你还记得早饭吃了豆腐脑和油条吗'})
            absent = gates.one({**scope, 'text':'你还记得火星深海灯塔吗'})
            absent_cases = [dict(text=text, result=gates.one({**scope, 'text':text})) for text in ABSENT_QUERIES]
            hit = gates.two({**scope, 'text':'早饭吃了豆腐脑'})
            miss = gates.two({**scope, 'text':'好'})
            missing_cost = gates.four({**scope, 'text':'平板电脑换屏幕重新预约', 'context_ts':time.time(),
                'why_now':'现在再次讨论平板电脑换屏幕', 'request_id':'smoke-missing-cost'})
            proposal = dict(scope, text='手机换电池重新预约', context_ts=time.time(),
                why_now='现在再次讨论同一手机换电池的预约', request_id='smoke-0')
            first = gates.four(proposal)
            duplicate = gates.four(proposal)
            decisions = [first] + [gates.four({**proposal, 'request_id':'smoke-' + str(i)}) for i in range(1,4)]
            audit = gates.audit(scope)['decisions']
            with next(Path(memory.raw.root).glob('*.jsonl')).open(encoding='utf8') as stream:
                raw = {row['id']: row for row in map(json.loads, stream)}
            assert found['state'] == 'found' and found['evidence']['quote'] in raw[found['evidence']['raw_id']]['t']
            assert suspected['state'] == 'suspected' and len(suspected['parts']) == 3
            assert suspected['parts'][2] == '所以我只能说到这儿'
            assert absent['state'] == 'absent' and absent['searched']['scope'] == scope
            assert all(case['result']['state'] == 'absent' and case['result']['searched']['scope'] == scope for case in absent_cases)
            assert hit['matched'] and len(hit['hits'][0]['overlap']) >= 6
            assert not miss['matched']
            assert all(d['allowed'] for d in decisions[:3])
            assert not decisions[3]['allowed'] and decisions[3]['reason_code'] == 'weekly_limit'
            assert audit[0]['reason_code'] == 'weekly_limit'
            assert not duplicate['allowed'] and duplicate['reason_code'] == 'duplicate_request'
            assert not missing_cost['allowed'] and missing_cost['reason_code'] == 'no_same_object_action_cost'
            assert missing_cost['used_this_week'] == 0
            module = Path(__file__).with_name('memory_gates.py')
            return dict(result='SMOKE_OK', isolated=True, elapsed_seconds=round(time.monotonic()-started,3),
                runtime_source=str(module), runtime_sha256=hashlib.sha256(module.read_bytes().replace(b'\r\n',b'\n')).hexdigest(),
                gate1=dict(found=found, suspected=suspected, absent=absent, absent_cases=absent_cases), gate2=dict(hit=hit, miss=miss),
                gate4=dict(first_three_allowed=True, allowed=first, duplicate=duplicate, missing_cost=missing_cost,
                           fourth=decisions[3], audit_last=audit[0]))
        finally:
            memory.store.close()
