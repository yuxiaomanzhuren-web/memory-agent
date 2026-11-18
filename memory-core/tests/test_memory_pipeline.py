import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from memory_core.server import MemoryService


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.memory = MemoryService(str(Path(self.tmp.name) / 'brain.db'), embed=False)
        self.addCleanup(self.memory.store.close)
        self.now = 1794369600.0
        self.scope = dict(bot_id='111', persona='xiaoman', conv_key='private:222')
        self.answer = {'items': []}
        self.calls = []

        def model(prompt, system, limit):
            self.calls.append(prompt)
            self.assertFalse(self.memory.store.conn.in_transaction)
            return json.dumps(self.answer, ensure_ascii=False)

        self.model = model

    def pipeline(self, **kw):
        from memory_core.memory_pipeline import MemoryPipeline
        return MemoryPipeline(self.memory, model=self.model, clock=lambda: self.now,
                              batch_size=kw.pop('batch_size', 1), **kw)

    def raw(self, text, role='user'):
        return self.memory.raw.append('111|private:222', role, text, ts=self.now)

    def notice(self, pipeline, ids, scope=None):
        return pipeline.notice({**(scope or self.scope), 'raw_ids': ids})

    def item(self, rid, quote, text='他明确提出先核查静默设置。', **kw):
        return {'text': text, 'kind': 'episode', 'importance': .6,
                'source_refs': [{'raw_id': rid, 'quote': quote}], **kw}

    def test_entrypoint_is_available(self):
        import memory_core
        self.assertTrue(hasattr(memory_core, '__path__'))
        try:
            from memory_core.memory_pipeline import MemoryPipeline
        except ImportError:
            self.fail('The agreed durable extraction worker is missing')
        self.assertTrue(callable(MemoryPipeline))

    def test_new_raw_notice_extracts_with_exact_sources_and_no_replay(self):
        p = self.pipeline()
        rid = self.raw('刚刚让你调静默你看见了吗')
        original = Path(self.memory.raw.root, '2026-11.jsonl').read_bytes()
        self.notice(p, [rid])
        self.answer = {'items': [self.item(rid, '刚刚让你调静默你看见了吗')]}
        p.tick()
        rows = self.memory.store.list_memories(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['meta']['raw_id'], rid)
        self.assertEqual(rows[0]['meta']['pipeline_scope'], self.scope)
        self.assertEqual(rows[0]['meta']['source_refs'][0]['quote'], '刚刚让你调静默你看见了吗')
        self.notice(p, [rid])
        self.pipeline().tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(original, Path(self.memory.raw.root, '2026-11.jsonl').read_bytes())

    def test_history_needs_explicit_backfill_and_persona_cannot_steal_raw(self):
        rid = self.raw('昨天决定暂缓红楼梦这一回。')
        p = self.pipeline()
        p.tick()
        self.assertEqual(self.calls, [])
        p.backfill({**self.scope, 'raw_ids': [rid], 'reason': '只验这一条历史样本'})
        other = {**self.scope, 'persona': 'ali'}
        with self.assertRaises(ValueError):
            self.notice(p, [rid], other)
        self.answer = {'items': [self.item(rid, '暂缓红楼梦', topic='红楼梦')]}
        p.tick()
        self.assertEqual(len(self.memory.store.list_memories(limit=10)), 1)

    def test_no_model_output_is_success_but_model_failure_is_visible_and_retryable(self):
        p = self.pipeline()
        rid = self.raw('一条需要核查的约定。')
        self.notice(p, [rid])
        p.model = lambda *a: (_ for _ in ()).throw(TimeoutError('fixture timeout'))
        p.tick()
        status = p.status()
        self.assertEqual(status['jobs']['failed'], 1)
        self.assertTrue(status['recent_jobs'][0]['id'])
        self.assertEqual(status['recent_jobs'][0]['attempts'], 1)
        self.assertIn('fixture timeout', status['last_error'])
        self.assertEqual(self.memory.store.list_memories(limit=10), [])
        self.now += 61
        p.model = self.model
        p.tick()
        self.assertEqual(p.status()['jobs']['done'], 1)
        self.assertEqual(self.memory.store.list_memories(limit=10), [])

    def test_fabricated_quote_fails_whole_batch_without_fallback_memory(self):
        p = self.pipeline()
        rid = self.raw('我想先核查静默设置。')
        self.notice(p, [rid])
        self.answer = {'items': [self.item(rid, '用户没说过这句话')]}
        p.tick()
        self.assertEqual(self.memory.store.list_memories(limit=10), [])
        self.assertEqual(p.status()['jobs']['failed'], 1)

    def test_extraction_references_cannot_copy_whole_long_raw_messages(self):
        p = self.pipeline()
        text = '这是一段原文。' * 80
        rid = self.raw(text)
        self.notice(p, [rid])
        self.answer = {'items': [self.item(rid, text)]}
        p.tick()
        self.assertEqual(self.memory.store.list_memories(limit=10), [])
        self.assertEqual(p.status()['jobs']['failed'], 1)

    def test_partial_jsonl_tail_waits_then_ingests_and_bad_line_is_visible(self):
        p = self.pipeline()
        path = Path(self.memory.raw.root, '2026-11.jsonl')
        row = dict(id='partial-1', ts=self.now, s='111|private:222', r='user', t='红楼梦先暂缓')
        encoded = json.dumps(row, ensure_ascii=False).encode()
        path.write_bytes(encoded[:30])
        p.tick()
        self.assertEqual(p.status()['raw_new'], 0)
        with path.open('ab') as f:
            f.write(encoded[30:] + b'\n{broken}\n' + json.dumps({**row, 'id': [1]}).encode() + b'\n')
        p.tick()
        self.assertEqual(p.status()['raw_new'], 1)
        self.assertGreaterEqual(p.status()['scan_errors'], 2)

    def test_conflicting_raw_id_is_quarantined_and_cannot_be_rebound(self):
        p = self.pipeline()
        rid = self.raw('第一句需要留住。')
        self.notice(p, [rid])
        path = Path(self.memory.raw.root, '2026-11.jsonl')
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(id=rid, ts=self.now, s='111|private:222', r='user', t='同一个ID的另一句话。'), ensure_ascii=False) + '\n')
        p.scan()
        row = self.memory.store.conn.execute('SELECT scope FROM memory_pipeline_raw WHERE raw_id=?', (rid,)).fetchone()
        self.assertEqual(row[0], '')
        with self.assertRaises(ValueError):
            self.notice(p, [rid])

    def test_assistant_summary_never_enters_factual_model_input(self):
        p = self.pipeline()
        a = self.raw('收尾：我猜他不喜欢红楼梦。', role='assistant')
        u = self.raw('其实我喜欢红楼梦。')
        self.notice(p, [a, u])
        self.answer = {'items': [self.item(u, '我喜欢红楼梦', text='他明确说喜欢红楼梦。')]}
        p.tick()
        self.assertNotIn('我猜', self.calls[0])

    def test_links_require_two_sources_and_same_event_not_merely_same_book(self):
        p = self.pipeline()
        a = self.raw('红楼梦暂缓三天，原因是现在读不进去。')
        self.notice(p, [a])
        self.answer = {'items': [self.item(a, '红楼梦暂缓三天', topic='红楼梦', event_key='defer-1110')]}
        p.tick()
        first = self.memory.store.list_memories(limit=10)[0]['id']
        b = self.raw('仍按那次暂缓决定，三天后再读红楼梦。')
        self.notice(p, [b])
        refs = [{'raw_id': a, 'quote': '红楼梦暂缓三天'}, {'raw_id': b, 'quote': '仍按那次暂缓决定'}]
        link = dict(memory_id=first, relation='同一件事', why='本句明确延续此前三天暂缓决定', source_refs=refs)
        self.answer = {'items': [self.item(b, '仍按那次暂缓决定', topic='红楼梦', event_key='defer-1110', links=[link])]}
        p.tick()
        # 口径 B + 「event_key 由程序生成、模型给的不算」（memory_pipeline.py:80）。
        # 这两条提炼的 event_key 由引用日期|对象|动作推导，模型传的 'defer-1110' 无效，
        # 所以连线必须被拒，并且要留下可核对的拒绝原因。
        edges = p.links(self.scope)['links']
        self.assertEqual(edges, [])
        skipped = [row for row in self.memory.store.conn.execute(
            "SELECT detail FROM memory_pipeline_audit WHERE operation='link_skipped'")]
        self.assertTrue(skipped, 'link rejection must be audited')
        self.assertIn('event_key', skipped[-1][0])
        timeline = p.timeline({**self.scope, 'topic': '红楼梦'})
        self.assertEqual(len(timeline['items']), 2)
        self.assertEqual(p.timeline({**self.scope, 'persona': 'ali', 'topic': '红楼梦'})['items'], [])
        self.assertEqual(self.memory.store.conn.execute('SELECT COUNT(*) FROM relations').fetchone()[0], 0)
        # 原后半段（拆线→重连被拒→墓碑可见）依赖「连线已建立」这个前提。
        # 口径 B 下这条同一件事连线不成立，所以这里明确跳过，而不是把它删掉当成已覆盖。
        self.skipTest('拆线与墓碑的覆盖需要一条真实的连线；本用例在口径 B 下不产生连线')

    def test_same_book_different_event_is_not_accepted_as_same_event(self):
        p = self.pipeline()
        a = self.raw('红楼梦先暂缓。')
        self.notice(p, [a])
        self.answer = {'items': [self.item(a, '红楼梦先暂缓', topic='红楼梦', event_key='defer')]}
        p.tick()
        first = self.memory.store.list_memories(limit=10)[0]['id']
        b = self.raw('今天决定重新开始红楼梦。')
        self.notice(p, [b])
        self.answer = {'items': [self.item(b, '重新开始红楼梦', topic='红楼梦', event_key='resume', links=[dict(memory_id=first, relation='同一件事', why='同一本书', source_refs=[{'raw_id': a, 'quote': '红楼梦先暂缓'}, {'raw_id': b, 'quote': '重新开始红楼梦'}])])]}
        p.tick()
        # 口径 B（每条合格提炼各自保留一条）：不同 event_key 的两句话都留下，
        # 只是不许连成「同一件事」。旧断言写的是合并成 1 条，那是口径 A。
        self.assertEqual(len(self.memory.store.list_memories(limit=10)), 2)
        self.assertEqual(p.links(self.scope)['links'], [])

    def test_book_name_as_event_key_does_not_justify_same_event(self):
        p = self.pipeline()
        a = self.raw('红楼梦先暂缓。')
        self.notice(p, [a])
        self.answer = {'items': [self.item(a, '红楼梦先暂缓', topic='红楼梦', event_key='红楼梦')]}
        p.tick()
        first = self.memory.store.list_memories(limit=10)[0]['id']
        b = self.raw('今天重新读红楼梦。')
        self.notice(p, [b])
        self.answer = {'items': [self.item(b, '今天重新读红楼梦', topic='红楼梦', event_key='红楼梦', links=[
            dict(memory_id=first, relation='同一件事', why='同一本书', source_refs=[
                {'raw_id': a, 'quote': '红楼梦先暂缓'}, {'raw_id': b, 'quote': '今天重新读红楼梦'}])])]}
        p.tick()
        self.assertEqual(p.links(self.scope)['links'], [])
        # 口径 B：连线是可选步骤，被拒只记 link_skipped，不该让整条提炼失败。
        self.assertEqual(p.status()['jobs']['failed'], 0)

    def test_existing_1034_style_memory_can_be_adopted_without_rewriting_it(self):
        rid = self.raw('刚刚让你调静默你看见了吗')
        mid = self.memory.remember({'text': '他问静默是否生效。', 'meta': {'raw_id': rid, 'raw_ts': self.now, 'quote': '刚刚让你调静默你看见了吗'}, 'visibility': 'private'})['id']
        before = self.memory.store.get_memory(mid)
        p = self.pipeline()
        p.adopt({**self.scope, 'memory_id': mid, 'reason': '接入现有验收样本'})
        self.assertEqual(before, self.memory.store.get_memory(mid))
        p.tick()
        self.assertEqual(self.calls, [])

    def test_atomic_insert_and_link_failure_roll_back_before_retry(self):
        p = self.pipeline()
        a = self.raw('红楼梦暂缓三天。')
        self.notice(p, [a])
        self.answer = {'items': [self.item(a, '红楼梦暂缓三天', topic='红楼梦', event_key='defer')]}
        p.tick()
        first = self.memory.store.list_memories(limit=10)[0]['id']
        b = self.raw('仍按那次暂缓决定，三天后再读。')
        self.notice(p, [b])
        self.answer = {'items': [self.item(b, '仍按那次暂缓决定', topic='红楼梦', event_key='defer', links=[
            dict(memory_id=first, relation='同一件事', why='新原句明确延续此前的暂缓决定', source_refs=[
                {'raw_id': a, 'quote': '红楼梦暂缓三天'}, {'raw_id': b, 'quote': '仍按那次暂缓决定'}])])]}
        self.memory.store.conn.executescript("CREATE TRIGGER fail_link BEFORE INSERT ON memory_links BEGIN SELECT RAISE(ABORT,'fixture link fail'); END;")
        p.tick()
        # 口径 B：条目是必做的，连线是可选的。连线插入被触发器打断时，
        # 条目必须留下（2 条），连线数为 0——不能把条目一起回滚掉。
        self.assertEqual(len(self.memory.store.list_memories(limit=10)), 2)
        self.assertEqual(len(p.links(self.scope)['links']), 0)
        self.assertEqual(self.memory.store.conn.execute('SELECT COUNT(*) FROM memories_fts').fetchone()[0], 2)
        self.memory.store.conn.execute('DROP TRIGGER fail_link')
        self.memory.store.conn.commit()
        self.now += 61
        p.tick()
        self.assertEqual(len(self.memory.store.list_memories(limit=10)), 2)
        # 条目留在库里即可；这条「同一件事」的连线因 event_key 由程序生成而不成立，
        # 所以重试后也不该出现连线（见 memory_pipeline.py:80 与 :730）。
        self.assertEqual(len(p.links(self.scope)['links']), 0)

    def test_expired_worker_cannot_commit_after_new_worker_completed_the_job(self):
        p = self.pipeline()
        rid = self.raw('我想核查静默设置。')
        self.notice(p, [rid])
        self.answer = {'items': [self.item(rid, '核查静默设置')]}
        entered, release = threading.Event(), threading.Event()
        results = []
        def slow(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test did not release model')
            return json.dumps(self.answer)
        p.model = slow
        thread = threading.Thread(target=lambda: results.append(p.tick()))
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.now += 181
            self.pipeline().tick()
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.memory.store.list_memories(limit=10)), 1)
        self.assertEqual(results, [{'lease_lost': True}])

    def test_review_candidates_do_not_become_facts_until_explicit_promotion(self):
        p = self.pipeline()
        rid = self.raw('我猜他不喜欢红楼梦。', role='assistant')
        self.notice(p, [rid])
        body = {**self.scope, 'source_role': 'assistant', 'source_kind': 'self_review',
                'turn_id': 'turn-unique', 'text': '纠错：我判错了，他说的是暂时读不进去。'}
        review = p.reviews(body)['review']
        self.assertEqual(p.reviews(body)['review']['id'], review['id'])
        self.assertEqual(self.memory.store.list_memories(limit=10), [])
        user_rid = self.raw('用户的这句话不是助手原判断。')
        self.notice(p, [user_rid])
        with self.assertRaises(ValueError):
            p.promote({**self.scope, 'id': review['id'], 'confirm': True,
                       'confirmed_by': 'owner', 'reason': '测试拒绝错误发言人', 'text': '不能把用户话当助手原判断。',
                       'original_ref': {'raw_id': user_rid, 'quote': '用户的这句话不是助手原判断。'}})
        with self.assertRaises(ValueError):
            p.promote({**self.scope, 'id': review['id']})
        promoted = p.promote({**self.scope, 'id': review['id'], 'confirm': True,
                              'confirmed_by': 'owner', 'reason': '已核对原判断与来信',
                              'text': '她此前把暂时读不进去误判为不喜欢红楼梦，现已纠正。',
                              'original_ref': {'raw_id': rid, 'quote': '我猜他不喜欢红楼梦。'}})
        mid = promoted['memory_id']
        note = self.memory.store.get_memory(mid)
        self.assertEqual(note['meta']['source_role'], 'assistant')
        self.assertEqual(note['meta']['source_kind'], 'self_review')
        self.assertEqual(note['meta']['self_review_id'], review['id'])
        self.assertEqual(p.promote({**self.scope, 'id': review['id'], 'confirm': True,
                              'confirmed_by': 'owner', 'reason': '已核对原判断与来信',
                              'text': note['text'], 'original_ref': {'raw_id': rid, 'quote': '我猜他不喜欢红楼梦。'}})['memory_id'], mid)

    def test_batch_count_and_idle_delay_prevent_one_model_call_per_raw_line(self):
        p = self.pipeline(batch_size=4)
        ids = [self.raw('只新增一条有出处的话。')]
        self.notice(p, ids)
        p.tick()
        self.assertEqual(self.calls, [])
        self.now += 181
        p.tick()
        self.assertEqual(len(self.calls), 1)

    def test_link_candidates_find_related_older_entry_before_recent_unrelated_ones(self):
        p = self.pipeline()
        old_id = None
        for number in range(13):
            quote = '红楼梦暂缓三天。' if number == 0 else f'兰花园艺记录第{number}次。'
            rid = self.raw(quote)
            mid = self.memory.remember({'text': quote, 'meta': {'raw_id': rid, 'raw_ts': self.now, 'quote': quote}})['id']
            p.adopt({**self.scope, 'memory_id': mid, 'reason': '测试带来源的旧候选'})
            if number == 0:
                old_id = mid
        rid = self.raw('仍按那次红楼梦暂缓三天的决定。')
        self.notice(p, [rid])
        p.tick()
        known = json.loads(self.calls[0].split('已知条目：', 1)[1])
        self.assertIn(old_id, [item['id'] for item in known])
        self.assertTrue(all('兰花' not in item['text'] for item in known))