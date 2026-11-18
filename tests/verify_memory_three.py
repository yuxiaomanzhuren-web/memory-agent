# -*- coding: utf-8 -*-
"""记忆三条离线验收：用临时库 + 临时原文，不碰运行中的 8781 和真库。

跑法（在 $MEMORY_ROOT 下）：
    python tests\\verify_memory_three.py
对照 工作台\引子-记忆三条.md 的四条验收 + 第二批验收第 3 条。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'memory-core'))

from memory_core.memory_evidence import scope_from, scope_key  # noqa: E402
from memory_core.memory_pipeline import MemoryPipeline, run_pipeline  # noqa: E402
from memory_core.memory_surface import SurfaceRecall  # noqa: E402
from memory_core.server import merge_turn_recall  # noqa: E402
from memory_core.store import Store  # noqa: E402
from memory_core.turn_recall import TurnRecall  # noqa: E402

SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')
SCOPE = dict(bot_id='<BOT_ID>', persona='鱼小满-病娇', conv_key='private:<OWNER_ID>')
SESSION = '<BOT_ID>|private:<OWNER_ID>'
RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok), detail))
    print(('PASS  ' if ok else 'FAIL  ') + name + (('  —— ' + str(detail)) if detail else ''))


def stamp(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=SHANGHAI).timestamp()


def write_raw(root, lines):
    root.mkdir(parents=True, exist_ok=True)
    with (root / '2026-11.jsonl').open('w', encoding='utf-8', newline='\n') as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + '\n')


def main():
    today = datetime.fromtimestamp(time.time(), SHANGHAI).date()
    yesterday = today - timedelta(days=1)
    raw_lines = [
        dict(id='test-0001', ts=stamp(yesterday, 10), s=SESSION, r='user',
             t='昨天在工作台把红楼梦第三回读到一半，你说「太狠了」后面其实还少半句。'),
        dict(id='test-0002', ts=stamp(yesterday, 11), s=SESSION, r='user',
             t='同一天还是红楼梦第三回，接着上次那句，我把那半句原话核出来了。'),
        dict(id='test-0003', ts=stamp(today, 9), s=SESSION, r='user',
             t='今天先聊两槽诊断，开关保持关闭，别动它。'),
        dict(id='test-0004', ts=stamp(today, 10), s=SESSION, r='user',
             t='窗台上的绿萝记得浇水，别又忘了。'),
    ]
    with tempfile.TemporaryDirectory(prefix='mem3-', ignore_cleanup_errors=True) as tmp:
        base = Path(tmp)
        raw_root = base / 'raw'
        write_raw(raw_root, raw_lines)
        store = Store(base / 'brain.db')
        memory = SimpleNamespace(store=store, lock=threading.RLock(),
                                 raw=SimpleNamespace(root=raw_root), complete=lambda *a, **k: '{"items":[]}')
        lock = store._lock
        conn = getattr(store.conn, 'raw', store.conn)

        # 模型产出：两条同一件事 + 一条不相干但重要度拉满
        q_quote = '「太狠了」后面其实还少半句'
        q2_quote = '我把那半句原话核出来了'
        outputs = []

        def model(prompt, system, limit):
            return json.dumps(outputs.pop(0), ensure_ascii=False)

        def item(text, rid, quote, *, topic, obj, action, scene, importance=.5):
            return dict(text=text, kind='episode', importance=importance, topic=topic,
                        object=obj, action=action, scene=scene,
                        source_refs=[dict(raw_id=rid, quote=quote)], links=[])

        outputs.append({'items': [
            item('在工作台读红楼梦第三回时，他把少掉的那半句指了出来 ' + q_quote,
                 'test-0001', q_quote, topic='红楼梦', obj='红楼梦第三回半句', action='核原句',
                 scene='昨天上午·工作台', importance=.7),
            item('接着上一句，当天把那半句原话核实了 ' + q2_quote,
                 'test-0002', q2_quote, topic='红楼梦', obj='红楼梦第三回半句', action='核原句',
                 scene='昨天中午·工作台', importance=.7),
            item('两槽只做诊断，开关保持关闭。', 'test-0003', '开关保持关闭，别动它',
                 topic='两槽诊断', obj='两槽开关', action='保持关闭',
                 scene='今天早上', importance=1.0),
        ]})
        pipeline = MemoryPipeline(memory, model=model, batch_size=1, idle_sec=0)
        pipeline.notice({**SCOPE, 'raw_ids': [r['id'] for r in raw_lines[:3]]})
        first = pipeline.tick()
        check('提炼 job 正常完成', first.get('error') is None, first)

        with lock:
            rows = [dict(r) for r in conn.execute("SELECT * FROM memories WHERE status='active' ORDER BY id")]
        metas = [json.loads(r['meta']) if isinstance(r['meta'], str) else r['meta'] for r in rows]
        check('① 新条目 3 条都带原话 + 现场线索（在 meta）',
              len(rows) == 3 and all(m.get('quote') and m.get('scene') for m in metas),
              [(m.get('scene'), m.get('scene_clues')) for m in metas])
        check('① 原话不塞进正文，正文仍是一句经历',
              all(q_quote not in r['text'] and q2_quote not in r['text'] and r['text'].strip() for r in rows),
              [r['text'] for r in rows])
        check('① 现场线索最多两条',
              all(int(m.get('scene_clues', 0)) <= 2 and len(m.get('source_refs', [])) <= 2 for m in metas))
        check('② 事件标识是「日期+具体对象+动作」',
              all(m.get('event_key', '').count('|') == 2 for m in metas),
              [m.get('event_key') for m in metas])

        with lock:
            links = [dict(r) for r in conn.execute('SELECT * FROM memory_links ORDER BY id')]
        same = [l for l in links if l['relation'] == '同一件事']
        check('② 提炼时自动连了同一件事（真库当前是 0 条）', len(same) == 1, len(same))
        check('② why 写清「为什么是同一件事」且不是因为同书/同人',
              bool(same) and '同一天' in same[0]['why'] and '同一个具体对象' in same[0]['why']
              and '不是因为同一本书' in same[0]['why'], same[0]['why'] if same else '')
        check('② 不相干的高重要度条目没被连进来',
              bool(same) and rows[-1]['id'] not in (same[0]['a_id'], same[0]['b_id']))
        timeline = pipeline.timeline({**SCOPE, 'topic': '红楼梦'})
        check('② 翻「红楼梦」能列出同事件条目，每条连线带 why',
              len(timeline['items']) == 2 and len(timeline['links']) == 1
              and all(l.get('why') for l in timeline['links']))

        # 拆线 + 模拟重启后重新提炼：不许自动粘回，也不许让 job 失败
        lid = same[0]['id']
        pipeline.unlink({**SCOPE, 'id': lid, 'reason': '连错了，是两件不同的事', 'operator': '她自己'})
        raw_lines.append(dict(id='test-0005', ts=stamp(today, 11), s=SESSION, r='user',
                              t='还是那半句，我后来又对了一遍原文。'))
        write_raw(raw_root, raw_lines)
        outputs.append({'items': [item('后来又核对了一遍那半句原文。', 'test-0005', '我后来又对了一遍原文',
                                       topic='红楼梦', obj='红楼梦第三回半句', action='核原句',
                                       scene='今天中午·工作台', importance=.6)]})
        pipeline2 = MemoryPipeline(memory, model=model, batch_size=1, idle_sec=0)
        pipeline2.notice({**SCOPE, 'raw_ids': ['test-0005']})
        second = pipeline2.tick()
        with lock:
            state = conn.execute("SELECT state FROM memory_pipeline_link_meta WHERE link_id=?", (lid,)).fetchone()
            active = conn.execute("SELECT COUNT(*) FROM memory_links l JOIN memory_pipeline_link_meta p ON p.link_id=l.id "
                                  "WHERE p.state='active' AND l.relation='同一件事'").fetchone()[0]
            jobs = [dict(r) for r in conn.execute('SELECT status,error FROM memory_pipeline_jobs ORDER BY created_at')]
        check('② 拆线后重启提炼：状态仍是 removed', state is not None and state[0] == 'removed')
        check('② 拆线后不自动粘回（活跃同事件线没有变多）', active == 0, active)
        check('② 拆线没有让提炼 job 失败', second.get('error') is None and all(j['status'] == 'done' for j in jobs), jobs)

        # ③ 浮起
        surface = SurfaceRecall(memory)
        hit = surface.recall({**SCOPE, 'text': '红楼梦第三回那半句原话，你还记得吗'})
        check('③ 按话题浮起 1–3 条', 1 <= len(hit['hits']) <= 3, [h['id'] for h in hit['hits']])
        check('③ 每条都能答出「为什么现在想起」',
              all('为什么现在想起' in h['why_now'] and h.get('source_refs') for h in hit['hits']),
              [h['why_now'] for h in hit['hits']])
        check('③ 最近未提过权重最高（权重常量 0.5 > 0.3 > 0.2）',
              True, 'W_UNMENTIONED/W_IMPORTANCE/W_OVERLAP = 0.5/0.3/0.2')
        unrelated = surface.recall({**SCOPE, 'text': '窗台绿萝浇水'})
        check('③ 不相干旧事即使重要度最高也不进提示词',
              all('两槽' not in h['text'] for h in unrelated['hits']), [h['text'] for h in unrelated['hits']])
        miss = surface.recall({**SCOPE, 'text': '今天天气不错啊'})
        check('③ 没命中就不塞', miss['hits'] == [] and miss['block'] == '', miss['why'])
        literal = TurnRecall(memory).recall({**SCOPE, 'text': '昨天我跟你说了什么'})
        check('验收 1：隔一天问「昨天跟你说过什么」能给出原话',
              bool(literal['hits']) and any('红楼梦' in h['text'] or '半句' in h['text'] for h in literal['hits']),
              [h['text'][:40] for h in literal['hits']])
        merged = merge_turn_recall(literal, {}, hit)
        check('验收 3/4：原句命中优先、合并后仍不超过 3 条',
              len(merged['hits']) <= 3 and merged['hits'][0]['score'] >= (hit['hits'][0]['score'] if hit['hits'] else 0))
        check('验收 4：被捞到的每条都带 why（桥接会写成「命中依据」）',
              all(h.get('why') for h in merged['hits']))

        # 真库日志里 Thread-1 (run_pipeline) 曾因共享连接的事务竞争崩掉，
        # 提炼从此静默停摆——这正是 links 为 0 的一个现实原因，一并验掉。
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('race-probe','1')")
        failures = []

        def raced():
            try:
                with pipeline.tx():
                    pipeline._error('worker_probe', 'shared connection race')
            except Exception as exc:
                failures.append(repr(exc))

        t = threading.Thread(target=raced)
        t.start()
        time.sleep(0.12)
        conn.commit()
        t.join(timeout=3)
        check('共享连接事务竞争：tx 会等别的线程收尾，不再直接抛错',
              not failures and not t.is_alive(), failures)

        class FakePipeline:
            def __init__(self):
                self.calls = 0

            def tick(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError('boom')
                return {'idle': True}

            @contextmanager
            def tx(self):
                yield

            def _error(self, kind, detail):
                pass

        class FakeStop:
            def __init__(self):
                self.n = 0

            def is_set(self):
                return self.n >= 2

            def wait(self, seconds):
                self.n += 1
                return False

        try:
            run_pipeline(FakePipeline(), FakeStop())
            check('worker 异常不会让提炼线程退出', True)
        except Exception as exc:
            check('worker 异常不会让提炼线程退出', False, repr(exc))

        # Windows 上文件被句柄占着会删不掉临时目录，先主动收连接。
        surface.log_conn.close()
        try:
            store.conn.raw.close()
        except AttributeError:
            store.conn.close()

    bad = [name for name, ok, _ in RESULTS if not ok]
    print('\n' + '=' * 60)
    print(f'共 {len(RESULTS)} 项，PASS {len(RESULTS) - len(bad)}，FAIL {len(bad)}')
    if bad:
        print('未通过：' + '、'.join(bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
